from __future__ import annotations
import asyncio
import logging
from typing import Optional

from binance.error import ClientError

from app.engine.exceptions import EngineBusyError, EngineFailure
from app.engine.models import Intent, IntentState, OrderRole, OrderStatus, Side
from app.engine.rounding import round_to_step, round_up_to_step
from app.exchange.errors import POST_ONLY_WOULD_CROSS, is_code
from app.exchange.filters import SymbolFilterCache
from app.exchange.rest import BinanceRestClient
from app.exchange.ws_userstream import UserDataStream
from app.persistence.repository import EventLogRepository, IntentOrderRepository, IntentRepository

log = logging.getLogger("engine")

_TERMINAL_ORDER_STATUSES = {"CANCELED", "EXPIRED", "REJECTED"}


class ExecutionEngine:
    """
    Управляет Intent-ами (задачами "довести позицию по символу до нужного состояния")
    через явную последовательность персистентных переходов состояния.

    Ожидание исполнения ордеров событийное — через UserDataStream (WebSocket).
    REST используется только как разовый safety-net запрос при таймауте
    (на случай пропущенного события при разрыве соединения), не как поллинг-цикл.
    """

    def __init__(
        self,
        rest: BinanceRestClient,
        filters: SymbolFilterCache,
        intents: IntentRepository,
        orders: IntentOrderRepository,
        events: EventLogRepository,
        user_stream: UserDataStream,
        symbol: str,
        qty_default: str,
        order_timeout_ms: int,
        close_timeout_ms: int,
        max_maker_attempts: int,
        max_close_retries: int,
        tp_pct: float,
        sl_pct: float,
    ) -> None:
        self.rest = rest
        self.filters = filters
        self.intents = intents
        self.orders = orders
        self.events = events
        self.user_stream = user_stream
        self.symbol = symbol.upper()
        self.qty_default = qty_default
        self.order_timeout_ms = order_timeout_ms
        self.close_timeout_ms = close_timeout_ms
        self.max_maker_attempts = max_maker_attempts
        self.max_close_retries = max_close_retries
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    async def handle_signal(self, side: Side, qty: Optional[str] = None) -> Intent:
        active = await self.intents.get_active(self.symbol)
        if active is not None:
            if active.state != IntentState.OPEN:
                raise EngineBusyError(
                    f"intent #{active.id} already active for {self.symbol} in state {active.state.value}"
                )
            # OPEN is a steady state, not "in-flight" — a new signal (including
            # a close/FLAT signal) supersedes it. Mark it resolved so the new
            # intent doesn't collide with the one-active-intent-per-symbol index;
            # the new intent's own _cancel_exits/_close_opposite steps handle
            # the actual exchange-side cleanup of the position it's superseding.
            await self.intents.update_state(active.id, IntentState.FLAT)

        qty_val = qty or self.qty_default
        intent = await self.intents.create(self.symbol, side, qty_val)
        await self.events.append("engine", "intent_created",
                                  {"symbol": self.symbol, "side": side.value, "qty": qty_val},
                                  intent_id=intent.id)
        await self._run_intent(intent.id)
        return await self.intents.get(intent.id)

    # ------------------------------------------------------------------ #
    # Main state machine driver
    # ------------------------------------------------------------------ #
    async def _run_intent(self, intent_id: int) -> None:
        intent = await self.intents.get(intent_id)
        assert intent is not None
        try:
            await self._cancel_exits(intent)
            await self._close_opposite(intent)

            if intent.desired_side == Side.FLAT:
                await self.intents.update_state(intent.id, IntentState.FLAT)
                await self.events.append("engine", "state_transition", {"to": "flat"}, intent.id)
                return

            await self._enter_position(intent)
            await self._place_exits(intent)
            await self.intents.update_state(intent.id, IntentState.OPEN)
            await self.events.append("engine", "state_transition", {"to": "open"}, intent.id)
        except EngineFailure as e:
            await self.intents.update_state(intent.id, IntentState.FAILED, failure_reason=str(e))
            await self.events.append("engine", "intent_failed", {"reason": str(e)}, intent.id)
            raise

    # ------------------------------------------------------------------ #
    # Step 1: cancel stale exits
    # ------------------------------------------------------------------ #
    async def _cancel_exits(self, intent: Intent) -> None:
        await self.intents.update_state(intent.id, IntentState.CANCELLING_EXITS)
        try:
            self.rest.cancel_all_open_orders(intent.symbol)
        except ClientError as e:
            log.warning(f"[CANCEL EXITS] cancel_all_open_orders failed (ignored): {e}")
        try:
            self.rest.cancel_all_algo_orders(intent.symbol)
        except ClientError as e:
            log.warning(f"[CANCEL EXITS] cancel_all_algo_orders failed (ignored): {e}")
        await self.events.append("engine", "state_transition", {"to": "cancelling_exits"}, intent.id)

    # ------------------------------------------------------------------ #
    # Step 2: close opposite-side position if any
    # ------------------------------------------------------------------ #
    async def _close_opposite(self, intent: Intent) -> None:
        await self.intents.update_state(intent.id, IntentState.CLOSING_OPPOSITE)
        await self.events.append("engine", "state_transition", {"to": "closing_opposite"}, intent.id)

        symbol = intent.symbol
        step = float(self.filters.get(symbol)["stepSize"])
        side = intent.desired_side

        def needs_close(pos_amt: float) -> bool:
            if side == Side.LONG:
                return pos_amt < 0
            if side == Side.SHORT:
                return pos_amt > 0
            return pos_amt != 0  # FLAT: закрываем любую позицию

        pos_amt = self._get_position_amt(symbol)
        if not needs_close(pos_amt):
            return

        close_side = "BUY" if pos_amt < 0 else "SELL"
        attempts = 0
        while attempts < self.max_close_retries:
            pos_amt = self._get_position_amt(symbol)
            if not needs_close(pos_amt):
                return
            rem = abs(pos_amt)
            if rem <= step / 2:
                return

            attempts += 1
            seq = await self.intents.increment_attempt(intent.id)
            qty_str = round_to_step(rem, step)
            price = self._maker_price(symbol, close_side)
            cid = f"i{intent.id}-close-{seq}"
            await self.orders.create(intent.id, OrderRole.CLOSE_OPPOSITE, cid, close_side,
                                      "LIMIT", qty_str, price)

            try:
                self.rest.place_limit_post_only(symbol, close_side, qty_str, price,
                                                 reduce_only=True, new_client_order_id=cid)
                await self.orders.update_status(cid, OrderStatus.ACKED)
            except ClientError as e:
                if is_code(e, POST_ONLY_WOULD_CROSS):
                    mkt_cid = f"{cid}-mkt"
                    await self.orders.create(intent.id, OrderRole.CLOSE_OPPOSITE, mkt_cid,
                                              close_side, "MARKET", qty_str, None)
                    try:
                        self.rest.place_market(symbol, close_side, qty_str,
                                                reduce_only=True, new_client_order_id=mkt_cid)
                        filled = await self._wait_for_fill(mkt_cid, timeout_ms=int(self.close_timeout_ms * 0.5))
                        if filled or not needs_close(self._get_position_amt(symbol)):
                            return
                    except ClientError as e2:
                        log.warning(f"[CLOSE] market fallback failed: {e2}")
                    await asyncio.sleep(self.order_timeout_ms / 1000)
                    continue
                log.warning(f"[CLOSE] post-only place failed: {e}")
                await asyncio.sleep(self.order_timeout_ms / 1000)
                continue

            filled = await self._wait_for_fill(cid, timeout_ms=self.close_timeout_ms)
            if filled:
                return
            try:
                self.rest.cancel_order(symbol, orig_client_order_id=cid)
                await self.orders.update_status(cid, OrderStatus.CANCELED)
            except ClientError:
                pass
            await asyncio.sleep(self.order_timeout_ms / 1000)

        raise EngineFailure("failed to close opposite position in time")

    # ------------------------------------------------------------------ #
    # Step 3: enter new position (post-only maker, market fallback)
    # ------------------------------------------------------------------ #
    async def _enter_position(self, intent: Intent) -> None:
        symbol = intent.symbol
        side = intent.desired_side
        entry_side = "BUY" if side == Side.LONG else "SELL"
        step = self.filters.get(symbol)["stepSize"]

        qty_str = round_to_step(float(intent.qty), step)
        qty_str = self._ensure_min_notional(symbol, entry_side, qty_str)

        if self._position_reached(symbol, entry_side, float(qty_str)):
            return

        await self.intents.update_state(intent.id, IntentState.ENTRY_MAKER_PENDING)
        await self.events.append("engine", "state_transition", {"to": "entry_maker_pending"}, intent.id)

        for attempt in range(1, self.max_maker_attempts + 1):
            seq = await self.intents.increment_attempt(intent.id)
            price = self._maker_price(symbol, entry_side)
            cid = f"i{intent.id}-entry-{seq}"
            await self.orders.create(intent.id, OrderRole.ENTRY_MAKER, cid, entry_side,
                                      "LIMIT", qty_str, price)
            try:
                self.rest.place_limit_post_only(symbol, entry_side, qty_str, price,
                                                 reduce_only=False, new_client_order_id=cid)
                await self.orders.update_status(cid, OrderStatus.ACKED)
            except ClientError as e:
                log.warning(f"[OPEN maker] post-only rejected: {e} (attempt {attempt}/{self.max_maker_attempts})")
                await self.orders.update_status(cid, OrderStatus.REJECTED)
                await asyncio.sleep(self.order_timeout_ms / 1000)
                continue

            filled = await self._wait_for_fill(cid, timeout_ms=self.order_timeout_ms * 2)
            if filled:
                return
            try:
                self.rest.cancel_order(symbol, orig_client_order_id=cid)
                await self.orders.update_status(cid, OrderStatus.CANCELED)
            except ClientError:
                pass
            await asyncio.sleep(self.order_timeout_ms / 1000)

        # --- Market fallback for whatever remains ---
        await self.intents.update_state(intent.id, IntentState.ENTRY_MARKET_PENDING)
        await self.events.append("engine", "state_transition", {"to": "entry_market_pending"}, intent.id)

        remaining = self._remaining_to_target(symbol, entry_side, float(qty_str))
        if remaining <= float(step) / 2:
            return

        rem_str = round_to_step(remaining, step)
        rem_str = self._ensure_min_notional(symbol, entry_side, rem_str)
        seq = await self.intents.increment_attempt(intent.id)
        cid = f"i{intent.id}-entry-market-{seq}"
        await self.orders.create(intent.id, OrderRole.ENTRY_MARKET, cid, entry_side, "MARKET", rem_str, None)
        self.rest.place_market(symbol, entry_side, rem_str, reduce_only=False, new_client_order_id=cid)
        await self._wait_for_fill(cid, timeout_ms=self.order_timeout_ms * 5)

    # ------------------------------------------------------------------ #
    # Step 4: place TP/SL exit orders
    # ------------------------------------------------------------------ #
    async def _place_exits(self, intent: Intent) -> None:
        await self.intents.update_state(intent.id, IntentState.PLACING_EXITS)
        await self.events.append("engine", "state_transition", {"to": "placing_exits"}, intent.id)

        symbol = intent.symbol
        side = intent.desired_side
        entry_price = self._get_entry_price(symbol)
        await self.intents.update_state(intent.id, IntentState.PLACING_EXITS, entry_price=str(entry_price))

        if entry_price <= 0 or (self.tp_pct <= 0 and self.sl_pct <= 0):
            return

        tick = self.filters.get(symbol)["tickSize"]
        close_side = "SELL" if side == Side.LONG else "BUY"

        if self.tp_pct > 0:
            tp_price = entry_price * (1 + self.tp_pct) if side == Side.LONG else entry_price * (1 - self.tp_pct)
            tp_price_str = round_to_step(tp_price, tick)
            seq = await self.intents.increment_attempt(intent.id)
            cid = f"i{intent.id}-tp-{seq}"
            await self.orders.create(intent.id, OrderRole.TP, cid, close_side, "TAKE_PROFIT_MARKET",
                                      None, tp_price_str)
            try:
                self.rest.place_take_profit_market(symbol, close_side, tp_price_str, new_client_order_id=cid)
                await self.orders.update_status(cid, OrderStatus.ACKED)
            except ClientError as e:
                log.warning(f"[TP place] failed: {e}")
                await self.orders.update_status(cid, OrderStatus.REJECTED)

        if self.sl_pct > 0:
            sl_price = entry_price * (1 - self.sl_pct) if side == Side.LONG else entry_price * (1 + self.sl_pct)
            sl_price_str = round_to_step(sl_price, tick)
            seq = await self.intents.increment_attempt(intent.id)
            cid = f"i{intent.id}-sl-{seq}"
            await self.orders.create(intent.id, OrderRole.SL, cid, close_side, "STOP_MARKET",
                                      None, sl_price_str)
            try:
                self.rest.place_stop_market(symbol, close_side, sl_price_str, new_client_order_id=cid)
                await self.orders.update_status(cid, OrderStatus.ACKED)
            except ClientError as e:
                log.warning(f"[SL place] failed: {e}")
                await self.orders.update_status(cid, OrderStatus.REJECTED)

    # ------------------------------------------------------------------ #
    # Helpers: position/price/rounding
    # ------------------------------------------------------------------ #
    def _get_position_amt(self, symbol: str) -> float:
        for p in self.rest.get_position_risk(symbol) or []:
            if str(p.get("symbol", "")).upper() == symbol.upper():
                return float(p.get("positionAmt", 0) or 0)
        return 0.0

    def _get_entry_price(self, symbol: str) -> float:
        for p in self.rest.get_position_risk(symbol) or []:
            if str(p.get("symbol", "")).upper() == symbol.upper():
                return float(p.get("entryPrice", 0) or 0)
        return 0.0

    def _maker_price(self, symbol: str, side: str) -> str:
        bt = self.rest.book_ticker(symbol)
        bid, ask = float(bt["bidPrice"]), float(bt["askPrice"])
        tick_s = self.filters.get(symbol)["tickSize"]
        tick = float(tick_s)
        if side == "BUY":
            target = bid
            if target >= ask:
                target = ask - tick
        else:
            target = ask
            if target <= bid:
                target = bid + tick
        return round_to_step(target, tick_s)

    def _ensure_min_notional(self, symbol: str, side: str, qty_str: str) -> str:
        filters = self.filters.get(symbol)
        min_notional = float(filters["minNotional"])
        if min_notional <= 0:
            return qty_str
        bt = self.rest.book_ticker(symbol)
        price = float(bt["askPrice"] if side == "BUY" else bt["bidPrice"])
        q = float(qty_str)
        if price > 0 and (q * price) < min_notional:
            need = min_notional / price
            return round_up_to_step(need, filters["stepSize"])
        return qty_str

    def _position_reached(self, symbol: str, side: str, target_qty: float) -> bool:
        amt = self._get_position_amt(symbol)
        need = target_qty * 0.999
        return (amt >= need) if side == "BUY" else (-amt >= need)

    def _remaining_to_target(self, symbol: str, side: str, target_qty: float) -> float:
        amt = self._get_position_amt(symbol)
        current_same_dir = max(amt, 0.0) if side == "BUY" else max(-amt, 0.0)
        return max(0.0, target_qty - current_same_dir)

    # ------------------------------------------------------------------ #
    # Fill waiting — event-driven off the WebSocket user data stream.
    # A single REST query is used only as a safety net if the WS event
    # never arrives in time (e.g. a connection drop swallowed it), not as
    # a polling loop.
    # ------------------------------------------------------------------ #
    async def _wait_for_fill(self, client_order_id: str, timeout_ms: int) -> bool:
        order = await self.orders.get_by_client_order_id(client_order_id)
        if order is None:
            return False
        intent = await self.intents.get(order.intent_id)
        symbol = intent.symbol

        event = self.user_stream.waiter_for(client_order_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_ms / 1000)
            result = self.user_stream.result_for(client_order_id)
            return bool(result and result["status"] == "FILLED")
        except asyncio.TimeoutError:
            log.debug(f"[wait_for_fill] no WS event for {client_order_id} within {timeout_ms}ms, REST fallback check")
            try:
                resp = self.rest.get_order(symbol, orig_client_order_id=client_order_id)
                status = resp.get("status")
                filled_qty = resp.get("executedQty", "0")
                order_id = resp.get("orderId")
                if status == "FILLED":
                    await self.orders.update_status(client_order_id, OrderStatus.FILLED,
                                                      filled_qty=filled_qty, exchange_order_id=order_id)
                    return True
                if status in _TERMINAL_ORDER_STATUSES:
                    await self.orders.update_status(client_order_id, OrderStatus.CANCELED,
                                                      filled_qty=filled_qty, exchange_order_id=order_id)
            except ClientError as e:
                log.warning(f"[wait_for_fill] REST fallback query failed: {e}")
            return False
        finally:
            self.user_stream.clear_waiter(client_order_id)
