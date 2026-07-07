from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional

import websockets

from app.config import WS_BASE_URL, LISTEN_KEY_KEEPALIVE_SEC
from app.engine.models import IntentState, OrderRole, OrderStatus
from app.exchange.rest import BinanceRestClient
from app.persistence.repository import EventLogRepository, IntentOrderRepository, IntentRepository, ListenKeyRepository

log = logging.getLogger("ws_userstream")

_TERMINAL_STATUSES = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
_EXIT_ROLES = {OrderRole.TP.value, OrderRole.SL.value}

_STATUS_MAP = {
    "NEW": OrderStatus.ACKED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "EXPIRED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
}


class UserDataStream:
    """
    Слушает Binance Futures User Data Stream по WebSocket (listenKey), обновляет
    intent_orders/intents по мере поступления событий и будит движок вместо
    того, чтобы движок поллил REST в цикле.

    Не asyncio-обёртка над binance-futures-connector'ом (тот синхронный/thread-based) —
    работает напрямую через `websockets`, единый event loop с FastAPI.
    """

    def __init__(self, rest: BinanceRestClient, listen_keys: ListenKeyRepository,
                 orders: IntentOrderRepository, intents: IntentRepository,
                 events: EventLogRepository) -> None:
        self.rest = rest
        self.listen_keys = listen_keys
        self.orders = orders
        self.intents = intents
        self.events = events

        self._listen_key: Optional[str] = None
        self._waiters: dict[str, asyncio.Event] = {}
        self._results: dict[str, dict] = {}
        self._ws_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._stopping = False
        self.connected = False

    # ------------------------------------------------------------------ #
    # Public API used by the execution engine
    # ------------------------------------------------------------------ #
    def waiter_for(self, client_order_id: str) -> asyncio.Event:
        ev = self._waiters.get(client_order_id)
        if ev is None:
            ev = asyncio.Event()
            self._waiters[client_order_id] = ev
        return ev

    def result_for(self, client_order_id: str) -> Optional[dict]:
        return self._results.get(client_order_id)

    def clear_waiter(self, client_order_id: str) -> None:
        self._waiters.pop(client_order_id, None)
        self._results.pop(client_order_id, None)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._listen_key = self.rest.new_listen_key()
        await self.listen_keys.save(self._listen_key)
        self._ws_task = asyncio.create_task(self._run_forever())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        log.info("UserDataStream started")

    async def stop(self) -> None:
        self._stopping = True
        for t in (self._ws_task, self._keepalive_task):
            if t:
                t.cancel()
        if self._listen_key:
            try:
                self.rest.close_listen_key(self._listen_key)
            except Exception as e:
                log.warning(f"close_listen_key failed (ignored): {e}")

    async def _keepalive_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(LISTEN_KEY_KEEPALIVE_SEC)
            try:
                self.rest.renew_listen_key(self._listen_key)
                await self.listen_keys.mark_renewed()
                log.info("listenKey renewed")
            except Exception as e:
                log.warning(f"listenKey renew failed, recreating: {e}")
                try:
                    self._listen_key = self.rest.new_listen_key()
                    await self.listen_keys.save(self._listen_key)
                except Exception as e2:
                    log.error(f"listenKey recreate failed: {e2}")

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                # Binance's 2026-03-06 WS upgrade split streams into routed /public,
                # /market, /private endpoints; unrouted connections (the old bare
                # /ws/<listenKey> form) silently receive no private events at all.
                url = f"{WS_BASE_URL}/private/ws?listenKey={self._listen_key}&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE"
                async with websockets.connect(url, ping_interval=180, ping_timeout=600) as ws:
                    log.info("WS user data stream connected")
                    self.connected = True
                    backoff = 1.0
                    async for raw in ws:
                        await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                log.warning(f"WS disconnected: {e!r}; reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                try:
                    self._listen_key = self.rest.new_listen_key()
                    await self.listen_keys.save(self._listen_key)
                except Exception as e2:
                    log.error(f"listenKey recreate on reconnect failed: {e2}")

    # ------------------------------------------------------------------ #
    # Event dispatch
    # ------------------------------------------------------------------ #
    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        kind = msg.get("e", "unknown")
        try:
            await self.events.append("ws", kind, msg)
        except Exception as e:
            log.warning(f"events_log append failed (ignored): {e}")

        if kind == "ORDER_TRADE_UPDATE":
            await self._handle_order_trade_update(msg)
        elif kind == "listenKeyExpired":
            log.warning("listenKeyExpired event received; forcing reconnect")
            raise RuntimeError("listenKeyExpired")

    async def _handle_order_trade_update(self, msg: dict) -> None:
        o = msg.get("o", {})
        client_order_id = o.get("c")
        status = o.get("X")
        exec_type = o.get("x")
        exchange_order_id = o.get("i")
        filled_qty = o.get("z", "0")
        if not client_order_id:
            return

        # "n"/"N" (commission/commissionAsset) только осмысленны на реальном
        # исполнении трейда ("x":"TRADE") — на NEW/CANCELED там обычно "0".
        commission_delta = o.get("n") if exec_type == "TRADE" else None
        commission_asset = o.get("N") if exec_type == "TRADE" else None

        mapped = _STATUS_MAP.get(status)
        intent_order = await self.orders.get_by_client_order_id(client_order_id)
        if intent_order is not None and mapped is not None:
            await self.orders.update_status(client_order_id, mapped, filled_qty=filled_qty,
                                             exchange_order_id=exchange_order_id,
                                             commission_delta=commission_delta,
                                             commission_asset=commission_asset)

        if status in _TERMINAL_STATUSES:
            self._results[client_order_id] = {
                "status": status, "filled_qty": filled_qty, "exchange_order_id": exchange_order_id,
            }
            ev = self._waiters.get(client_order_id)
            if ev is not None:
                ev.set()

        # A TP/SL fill closes the position on the exchange's own initiative —
        # the local Intent needs to follow even though the engine wasn't
        # actively waiting on this clientOrderId.
        if status == "FILLED" and intent_order is not None and intent_order.role.value in _EXIT_ROLES:
            intent = await self.intents.get(intent_order.intent_id)
            if intent is not None and intent.state == IntentState.OPEN:
                await self.intents.update_state(intent.id, IntentState.FLAT)
                await self.events.append(
                    "engine", "auto_closed_by_exit_order",
                    {"role": intent_order.role.value, "client_order_id": client_order_id},
                    intent_id=intent.id,
                )
                log.info(f"[AUTO-CLOSE] intent #{intent.id} -> FLAT ({intent_order.role.value} filled on exchange)")
