from __future__ import annotations
import asyncio
import logging
from typing import Optional

from binance.error import ClientError

from app.engine.exceptions import EngineBusyError, EngineFailure
from app.engine.fees import solve_exit_price_for_net_pnl
from app.engine.models import Intent, IntentState, OrderRole, OrderStatus, Side
from app.engine.netting import compute_next_action, compute_target_position
from app.engine.rounding import round_to_step, round_up_to_step
from app.exchange.fees import CommissionRateCache
from app.exchange.filters import SymbolFilterCache
from app.exchange.market_stream import BookDepthRecorder
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
        leverage: int,
        commission_rates: Optional[CommissionRateCache] = None,
        book_recorder: Optional[BookDepthRecorder] = None,
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
        self.leverage = leverage
        self.commission_rates = commission_rates or CommissionRateCache(rest)
        self.book_recorder = book_recorder
        # Сериализует handle_signal целиком (busy-check + create + _run_intent)
        # для символа: без этого два почти одновременных сигнала (например,
        # ручной клик на дашборде вперемешку с сигналом стратегии) могли бы
        # оба пройти busy-check до того, как любой из них создаст свой Intent,
        # и создать два параллельных Intent-а для одного символа — ломая
        # инвариант "один линейный intent-chain на символ", на который
        # опирается netting.py и учёт комиссий. Один Lock, а не per-symbol
        # словарь: бот сейчас торгует ровно одним символом (self.symbol).
        self._signal_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    async def handle_signal(self, side: Side, qty: Optional[str] = None) -> Intent:
        async with self._signal_lock:
            active = await self.intents.get_active(self.symbol)
            if active is not None:
                if active.state != IntentState.OPEN:
                    raise EngineBusyError(
                        f"intent #{active.id} already active for {self.symbol} in state {active.state.value}"
                    )
                # OPEN is a steady state, not "in-flight" — a new signal (including
                # a close/FLAT signal) supersedes it. Mark it resolved so the new
                # intent doesn't collide with the one-active-intent-per-symbol index;
                # the new intent's own _cancel_exits/_reduce_position steps handle
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

            step = float(self.filters.get(intent.symbol)["stepSize"])
            if intent.plan_target_amt is None:
                # Первый проход этого intent-а: (side, qty) трактуется как
                # СИГНАЛ-ДЕЛЬТА относительно текущей позиции (не абсолютный
                # целевой размер) — считаем ЦЕЛЬ (знаковый target-position)
                # ОДИН раз и персистим немедленно. На повторном запуске
                # (после краха/рестарта) цель читается обратно, а НЕ
                # пересчитывается заново от текущей позиции — иначе часть
                # уже исполненной на бирже дельты потерялась бы.
                existing_amt = self._get_position_amt(intent.symbol)
                target_amt = compute_target_position(
                    existing_amt, intent.desired_side, float(intent.qty), step)
                await self.intents.set_plan(intent.id, str(target_amt))
            else:
                target_amt = float(intent.plan_target_amt)

            # А вот ЧТО делать прямо сейчас (close_qty/open_qty) безопасно
            # пересчитывать заново от ЖИВОЙ текущей позиции относительно
            # зафиксированной цели на КАЖДОМ проходе, в т.ч. после краха:
            # цель неизменна, текущая позиция всегда актуальна, а разница
            # между ними и есть ровно то, что осталось сделать — никакого
            # отдельного учёта "сколько уже закрыто в этом вызове" не нужно.
            current_amt = self._get_position_amt(intent.symbol)
            close_qty, open_qty = compute_next_action(current_amt, target_amt, step)

            if close_qty > step / 2:
                await self._reduce_position(intent, close_qty)

            if intent.desired_side == Side.FLAT:
                await self.intents.update_state(intent.id, IntentState.FLAT)
                await self.events.append("engine", "state_transition", {"to": "flat"}, intent.id)
                return

            if open_qty > step / 2:
                await self._enter_position(intent, open_qty)

            # Безусловно — в т.ч. после частичного сокращения (open_qty==0,
            # но позиция всё ещё открыта меньшим объёмом): старые TP/SL уже
            # снесены _cancel_exits, без этого позиция осталась бы без
            # защиты. Собственный guard entry_price<=0 внутри — корректный
            # no-op для случая полного закрытия.
            await self._place_exits(intent)
            final_amt = self._get_position_amt(intent.symbol)
            final_state = IntentState.OPEN if abs(final_amt) > step / 2 else IntentState.FLAT
            await self.intents.update_state(intent.id, final_state)
            await self.events.append("engine", "state_transition", {"to": final_state.value}, intent.id)
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
    # Step 2: reduce/close the position by an explicit qty (netting plan)
    # ------------------------------------------------------------------ #
    async def _reduce_position(self, intent: Intent, target_close_qty: float) -> None:
        """Рыночным reduce-only ордером сокращает позицию на target_close_qty
        (не 'весь противоположный остаток' — сколько закрыть решает вызывающий
        через compute_next_action). Всегда market, без post-only попытки —
        закрытие/защита существующей позиции важнее экономии на комиссии."""
        await self.intents.update_state(intent.id, IntentState.CLOSING_OPPOSITE)
        await self.events.append("engine", "state_transition", {"to": "closing_opposite"}, intent.id)

        symbol = intent.symbol
        step = float(self.filters.get(symbol)["stepSize"])

        start_amt = self._get_position_amt(symbol)
        start_abs = abs(start_amt)
        if start_abs <= step / 2:
            return
        close_side = "BUY" if start_amt < 0 else "SELL"

        def needs_close(pos_amt: float) -> bool:
            return pos_amt < 0 if close_side == "BUY" else pos_amt > 0

        for _ in range(self.max_close_retries):
            pos_amt = self._get_position_amt(symbol)
            if not needs_close(pos_amt):
                break
            remaining = target_close_qty - (start_abs - abs(pos_amt))
            if remaining <= step / 2:
                break
            qty_str = round_to_step(min(remaining, abs(pos_amt)), step)
            if float(qty_str) <= step / 2:
                break

            seq = await self.intents.increment_attempt(intent.id)
            cid = f"i{intent.id}-reduce-{seq}"
            await self.orders.create(intent.id, OrderRole.CLOSE_OPPOSITE, cid, close_side,
                                      "MARKET", qty_str, None)
            try:
                self.rest.place_market(symbol, close_side, qty_str,
                                        reduce_only=True, new_client_order_id=cid)
                await self._wait_for_fill(cid, timeout_ms=self.close_timeout_ms)
            except ClientError as e:
                log.warning(f"[REDUCE] market close failed: {e}")
                await asyncio.sleep(self.order_timeout_ms / 1000)
        else:
            final_amt = self._get_position_amt(symbol)
            if needs_close(final_amt) and abs(final_amt) > step / 2:
                raise EngineFailure("failed to reduce position in time")

    # ------------------------------------------------------------------ #
    # Step 3: enter/add qty in `side` direction (post-only maker, market fallback)
    # ------------------------------------------------------------------ #
    async def _enter_position(self, intent: Intent, qty: float) -> None:
        """Открывает/добавляет ровно qty в направлении intent.desired_side —
        qty здесь ВСЕГДА уже конкретная величина к исполнению (open_qty_final
        из netting-плана в _run_intent), не intent.qty напрямую."""
        symbol = intent.symbol
        side = intent.desired_side
        entry_side = "BUY" if side == Side.LONG else "SELL"
        step_s = self.filters.get(symbol)["stepSize"]
        step = float(step_s)

        start_amt = self._get_position_amt(symbol)

        def filled_so_far() -> float:
            # Знаковое изменение позиции с начала входа, спроецированное на
            # направление входа — сколько из qty уже реально исполнено на
            # бирже (переживает частичные фейлы/отмены между попытками).
            cur = self._get_position_amt(symbol)
            return (cur - start_amt) if entry_side == "BUY" else (start_amt - cur)

        await self.intents.update_state(intent.id, IntentState.ENTRY_MAKER_PENDING)
        await self.events.append("engine", "state_transition", {"to": "entry_maker_pending"}, intent.id)

        for attempt in range(1, self.max_maker_attempts + 1):
            remaining = qty - filled_so_far()
            if remaining <= step / 2:
                return
            bid, ask = self._get_book(symbol)
            qty_str = round_to_step(remaining, step_s)
            qty_str = self._ensure_min_notional(bid, ask, symbol, entry_side, qty_str)

            seq = await self.intents.increment_attempt(intent.id)
            price = self._maker_price(bid, ask, symbol, entry_side)
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

        remaining = qty - filled_so_far()
        if remaining <= step / 2:
            return

        bid, ask = self._get_book(symbol)
        rem_str = round_to_step(remaining, step_s)
        rem_str = self._ensure_min_notional(bid, ask, symbol, entry_side, rem_str)
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
        # Один REST-вызов вместо двух: entry_price и position_amt приходят в
        # одном и том же ответе get_position_risk, момент времени один и тот же.
        pos_amt, entry_price = self._get_position(symbol)
        await self.intents.update_state(intent.id, IntentState.PLACING_EXITS, entry_price=str(entry_price))

        if entry_price <= 0 or (self.tp_pct <= 0 and self.sl_pct <= 0):
            return

        # Реальная сторона ПОЗИЦИИ, не intent.desired_side — после частичного
        # сокращения (netting-план: close_qty>0, open_qty=0) они расходятся:
        # результат может остаться открытым в СТАРОМ направлении, а не в том,
        # что было в сигнале. TP/SL обязаны считаться от факта на бирже.
        side = Side.LONG if pos_amt > 0 else Side.SHORT
        tick = self.filters.get(symbol)["tickSize"]
        close_side = "SELL" if side == Side.LONG else "BUY"

        # Net-PnL-aware TP/SL: TP_PCT/SL_PCT задают ЧИСТЫЙ результат (после
        # обеих комиссий), а не просто % движения цены от входа. Вход мог
        # исполниться частично как maker, частично как market — берём
        # фактическую суммарную комиссию входа из WS, а не оценку по ставке.
        # Выход всегда taker (TP/SL — Algo Order, market-type).
        qty_actual = abs(pos_amt)
        entry_fee = await self.orders.sum_entry_commission(intent.id)
        try:
            taker_rate = self.commission_rates.get(symbol)["taker"]
        except Exception as e:
            log.warning(f"[EXITS] commission rate fetch failed, assuming 0: {e}")
            taker_rate = 0.0
        entry_notional = entry_price * qty_actual

        if self.tp_pct > 0:
            target_net = entry_notional * self.tp_pct
            tp_price = solve_exit_price_for_net_pnl(entry_price, qty_actual, entry_fee, taker_rate, target_net, side)
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
            target_net = -(entry_notional * self.sl_pct)
            sl_price = solve_exit_price_for_net_pnl(entry_price, qty_actual, entry_fee, taker_rate, target_net, side)
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
    def _get_position(self, symbol: str) -> tuple[float, float]:
        """(positionAmt, entryPrice) — из WS-кэша ACCOUNT_UPDATE (near-instant),
        REST get_position_risk только фолбэк (нет кэша/переподключение/гонка
        с ORDER_TRADE_UPDATE — см. UserDataStream.get_cached_position)."""
        cached = self.user_stream.get_cached_position(symbol)
        if cached is not None:
            return cached
        for p in self.rest.get_position_risk(symbol) or []:
            if str(p.get("symbol", "")).upper() == symbol.upper():
                return float(p.get("positionAmt", 0) or 0), float(p.get("entryPrice", 0) or 0)
        return 0.0, 0.0

    def _get_position_amt(self, symbol: str) -> float:
        return self._get_position(symbol)[0]

    def _get_book(self, symbol: str) -> tuple[float, float]:
        """(bid, ask) — из WS-кэша стакана (свежее REST на ~260ms), REST
        book_ticker только фолбэк (поток не подключён/данные протухли)."""
        if self.book_recorder is not None:
            cached = self.book_recorder.get_best_prices()
            if cached is not None:
                return cached
        bt = self.rest.book_ticker(symbol)
        return float(bt["bidPrice"]), float(bt["askPrice"])

    def _maker_price(self, bid: float, ask: float, symbol: str, side: str) -> str:
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

    def _ensure_min_notional(self, bid: float, ask: float, symbol: str, side: str, qty_str: str) -> str:
        filters = self.filters.get(symbol)
        min_notional = float(filters["minNotional"])
        if min_notional <= 0:
            return qty_str
        price = ask if side == "BUY" else bid
        q = float(qty_str)
        if price > 0 and (q * price) < min_notional:
            need = min_notional / price
            return round_up_to_step(need, filters["stepSize"])
        return qty_str

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
