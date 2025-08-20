from __future__ import annotations
import time, uuid
from typing import Literal, Dict, Any
from binance.error import ClientError
from binance_client import BinanceFutures
from utils import round_to_step, round_up_to_step

from config import TP_PCT, SL_PCT, POSTONLY_MARKET_AFTER
import logging

log = logging.getLogger("order_manager")
Side = Literal["BUY", "SELL"]

class OrderManager:
    def __init__(self, client: BinanceFutures, symbol: str, qty_default: float,
                 tick_size: str, step_size: str, order_timeout_ms: int, max_retries: int,
                 close_timeout_ms: int | None = None,
                 tp_enabled: bool = True, sl_enabled: bool = True,
                 min_notional: float | str = 0):
        self.client = client
        self.symbol = symbol
        self.qty_default = qty_default
        self.tick_size = tick_size
        self.step_size = step_size
        self.order_timeout_ms = order_timeout_ms
        self.max_retries = max_retries
        self.close_timeout_ms = close_timeout_ms or (self.order_timeout_ms * 2)
        # runtime-флаги для включения/выключения TP/SL
        self.tp_enabled = tp_enabled
        self.sl_enabled = sl_enabled
        # MIN_NOTIONAL из exchangeInfo (нужен в _ensure_min_notional_qty)
        try:
            self.min_notional = float(min_notional)
        except Exception:
            self.min_notional = 0.0




    def get_entry_price(self) -> float:
        """
        Берём entryPrice из позиций по символу.
        Если нет позиции — возвращаем 0.0
        """
        try:
            positions = self.client.get_positions(self.symbol.upper())
            for p in positions or []:
                if str(p.get("symbol", "")).upper() == self.symbol.upper():
                    return float(p.get("entryPrice", 0) or 0.0)
        except Exception as e:
            log.warning(f"[entryPrice] failed: {e}", exc_info=True)
        return 0.0

    def place_exit_orders(self, side: Side, entry_price: float, qty_str: str) -> Dict[str, Any]:
        """
        Ставит TP/SL триггерами MARKET с closePosition=True (на весь объём текущей позиции).
        side — сторона ТЕКУЩЕЙ ПОЗИЦИИ: BUY (лонг) или SELL (шорт).
        qty_str передаём для совместимости, но не используем (closePosition закрывает весь остаток).
        """
        placed: Dict[str, Any] = {"tp": None, "sl": None}

        # противоположная сторона для закрытия позиции
        close_side: Side = "SELL" if side == "BUY" else "BUY"

        # если entry_price не удалось прочитать — возьмём mid из стакана
        if not entry_price or entry_price <= 0:
            try:
                bt = self.client.book_ticker(self.symbol)
                bid = float(bt["bidPrice"])
                ask = float(bt["askPrice"])
                entry_price = (bid + ask) / 2.0
            except Exception:
                entry_price = 0.0

        tp_pct = float(TP_PCT or 0.0)
        sl_pct = float(SL_PCT or 0.0)

        # runtime-флаги + проценты из конфига
        tp_on = self.tp_enabled and (tp_pct > 0)
        sl_on = self.sl_enabled and (sl_pct > 0)

        # если оба выключены — быстро выходим
        if not tp_on and not sl_on:
            return placed

        # расчёт цен триггеров
        if entry_price > 0:
            if side == "BUY":  # long
                tp_price = entry_price * (1.0 + tp_pct) if tp_on else None
                sl_price = entry_price * (1.0 - sl_pct) if sl_on else None
            else:              # short
                tp_price = entry_price * (1.0 - tp_pct) if tp_on else None
                sl_price = entry_price * (1.0 + sl_pct) if sl_on else None
        else:
            tp_price = None
            sl_price = None

        # округление по tickSize
        tp_price_str = round_to_step(tp_price, self.tick_size) if tp_price else None
        sl_price_str = round_to_step(sl_price, self.tick_size) if sl_price else None

        # --- TP: TAKE_PROFIT_MARKET closePosition=True
        if tp_price_str:
            try:
                tp_cid = f"tp-{uuid.uuid4().hex[:10]}"
                tp = self.client.place_take_profit_market(
                    symbol=self.symbol,
                    side=close_side,
                    stop_price=tp_price_str,
                    new_client_order_id=tp_cid,
                )
                placed["tp"] = {"cid": tp_cid, "stopPrice": tp_price_str, "raw": tp}
                log.info(f"[TP] TAKE_PROFIT_MARKET placed: entry_side={side} close_side={close_side} stopPrice={tp_price_str}")
            except Exception as e:
                log.warning(f"[TP place] failed: {e}", exc_info=True)

        # --- SL: STOP_MARKET closePosition=True
        if sl_price_str:
            try:
                sl_cid = f"sl-{uuid.uuid4().hex[:10]}"
                sl = self.client.place_stop_market(
                    symbol=self.symbol,
                    side=close_side,
                    stop_price=sl_price_str,
                    new_client_order_id=sl_cid,
                )
                placed["sl"] = {"cid": sl_cid, "stopPrice": sl_price_str, "raw": sl}
                log.info(f"[SL] STOP_MARKET placed: entry_side={side} close_side={close_side} stopPrice={sl_price_str}")
            except Exception as e:
                log.warning(f"[SL place] failed: {e}", exc_info=True)

        return placed



    # -------- Price helpers for maker placement --------
    def maker_price(self, side: Side) -> str:
        bt = self.client.book_ticker(self.symbol)
        best_bid = float(bt["bidPrice"])
        best_ask = float(bt["askPrice"])
        tick = float(self.tick_size)

        if side == "BUY":
            target = best_bid
            if target >= best_ask:
                target = best_ask - tick
        else:
            target = best_ask
            if target <= best_bid:
                target = best_bid + tick
        return round_to_step(target, self.tick_size)

    def norm_qty(self, qty: float | None) -> str:
        q = qty if qty is not None else self.qty_default
        return round_to_step(q, self.step_size)

    def _ensure_min_notional_qty(self, price: float, qty_str: str) -> str:
        """
        Если qty*price < MIN_NOTIONAL — повышаем qty вверх к stepSize.
        """
        try:
            q = float(qty_str)
            if self.min_notional > 0 and price > 0 and (q * price) < self.min_notional:
                need = self.min_notional / price
                return round_up_to_step(need, self.step_size)
        except Exception:
            pass
        return qty_str


    # -------- Positions --------
    def get_position_amt(self) -> float:
        try:
            positions = self.client.get_positions(self.symbol.upper())
            log.debug(f"[positions] {self.symbol.upper()} -> {positions}")
            for p in positions or []:
                if str(p.get("symbol","")).upper() == self.symbol.upper():
                    return float(p.get("positionAmt", 0) or 0)
            return 0.0
        except Exception as e:
            log.error(f"[ERROR] Не удалось получить позиции: {e}", exc_info=True)
            raise

    def _wait_entry_info(self, timeout_ms: int = 7000):
        """
        Ждём пока после входа стабилизируется entryPrice/positionAmt.
        Таймаут увеличен до ~7с — у Binance обновление entryPrice иногда запаздывает.
        Если спустя таймаут entryPrice всё ещё 0, но позиция != 0 — вернём (amt, 0.0),
        а TP/SL просто не будем ставить (чтобы не ставить мусор).
        """
        deadline = self.client.now_ms() + timeout_ms
        last_amt = 0.0
        while self.client.now_ms() < deadline:
            try:
                pos = self.client.get_positions(self.symbol.upper()) or []
                for p in pos:
                    if str(p.get("symbol","")).upper() == self.symbol.upper():
                        amt = float(p.get("positionAmt", 0) or 0)
                        ep = float(p.get("entryPrice", 0) or 0)
                        last_amt = amt
                        if abs(amt) > 0 and ep > 0:
                            return (amt, ep)
            except Exception:
                pass
            time.sleep(0.05)
        return (last_amt, 0.0)


    # -------- Exit orders (TP/SL) --------
    def cancel_exit_orders(self):
        """
        Самый надежный способ на USD-M фьючерсах: снести все открытые ордера символа.
        Это уберёт старые TP/SL и любые подвисшие лимитки.
        """
        try:
            self.client.cancel_all_open_orders(self.symbol)
            log.info(f"[CANCEL EXITS] cancel_open_orders({self.symbol}) done")
        except Exception as e:
            log.warning(f"[CANCEL EXITS] cancel_open_orders failed: {e}", exc_info=True)



    def _exit_sides(self, entry_side: Side) -> Side:
        """Сторона закрытия позиции (противоположная входу)."""
        return "SELL" if entry_side == "BUY" else "BUY"

    def _exit_prices(self, entry_price: float, entry_side: Side):
        """Рассчитать стоп-цены для TP/SL и привести к tickSize."""
        if entry_price <= 0:
            return None, None
        tp_pct = float(TP_PCT or 0.0)
        sl_pct = float(SL_PCT or 0.0)
        if tp_pct <= 0 and sl_pct <= 0:
            return None, None

        if entry_side == "BUY":   # long
            tp = entry_price * (1 + tp_pct) if tp_pct > 0 else None
            sl = entry_price * (1 - sl_pct) if sl_pct > 0 else None
        else:                     # short
            tp = entry_price * (1 - tp_pct) if tp_pct > 0 else None
            sl = entry_price * (1 + sl_pct) if sl_pct > 0 else None

        tp_s = round_to_step(tp, self.tick_size) if tp else None
        sl_s = round_to_step(sl, self.tick_size) if sl else None
        return tp_s, sl_s


    # -------- Market helpers --------
    def close_market(self, side: Side):
        rem = abs(self.get_position_amt())
        if rem <= 0:
            return {"closed": False, "info": "flat"}
        q = round_to_step(rem, self.step_size)
        self.client.place_market(self.symbol, side, q, reduce_only=True)
        return {"closed": True, "info": "market close", "qty": q}

    def open_market(self, side: Side, qty: float | None = None):
        """
        Открывает позицию рыночным и сразу ставит TP/SL на весь объём.
        """
        qty_str = self.norm_qty(qty)

        # Подстрахуемся от MIN_NOTIONAL
        try:
            bt = self.client.book_ticker(self.symbol)
            bid = float(bt["bidPrice"]); ask = float(bt["askPrice"])
            price_for_check = ask if side == "BUY" else bid
            qty_str = self._ensure_min_notional_qty(price_for_check, qty_str)
        except Exception:
            pass

        # 1) открыть market
        self.client.place_market(self.symbol, side, qty_str, reduce_only=False)

        # 2) получить entryPrice
        ep = self.get_entry_price()

        # 3) выставить TP/SL
        exits = self.place_exit_orders(side, ep, qty_str)

        return {
            "filled": True,
            "price": None,
            "clientOrderId": None,
            "entryPrice": ep,
            "exits": exits,
            "mode": "market",
        }


    def open_postonly_maker(self, side: Side, qty: float | None = None, market_fallback_after: int = POSTONLY_MARKET_AFTER):
        """
        1) Пытаемся открыть лимитным Post-Only до market_fallback_after раз.
           На каждой попытке: ставим post-only, ждём исполнения (по факту позиции), иначе отменяем и репрайсим.
        2) Если не добрали целевой объём — принудительно добираем MARKET.
        3) В конце ставим TP/SL (на весь текущий объём) через closePosition=True.
        """
        qty_str = self.norm_qty(qty)

        # Стартовая подстраховка от MIN_NOTIONAL по текущему рынку
        try:
            bt = self.client.book_ticker(self.symbol)
            bid = float(bt["bidPrice"]); ask = float(bt["askPrice"])
            price_check = ask if side == "BUY" else bid
            qty_str = self._ensure_min_notional_qty(price_check, qty_str)
        except Exception:
            pass

        # Если целевой объём уже достигнут — просто ставим выходы
        if self._position_reached(side, float(qty_str)):
            ep = self.get_entry_price()
            exits = self.place_exit_orders(side, ep, qty_str)
            return {
                "filled": True,
                "attempts": 0,
                "price": None,
                "clientOrderId": None,
                "entryPrice": ep,
                "exits": exits,
                "mode": "maker",
            }

        # --- Пост-онли попытки ---
        for attempt in range(1, int(market_fallback_after) + 1):
            price = self.maker_price(side)
            cid = f"open-{uuid.uuid4().hex[:10]}"

            # Пересчитываем qty под конкретную цену попытки (MIN_NOTIONAL)
            qty_try = self._ensure_min_notional_qty(float(price), qty_str)

            placed = False
            try:
                self.client.place_limit_post_only(
                    self.symbol, side, qty_try, price,
                    reduce_only=False,
                    new_client_order_id=cid
                )
                placed = True
            except Exception as e:
                log.warning(f"[OPEN maker] post-only rejected: {e}. attempt={attempt}/{market_fallback_after}")
                time.sleep(self.order_timeout_ms / 1000)
                continue

            # Ждём набора позиции
            deadline = self.client.now_ms() + (self.order_timeout_ms * 2)
            while self.client.now_ms() < deadline:
                if self._position_reached(side, float(qty_try)):
                    ep = self.get_entry_price()
                    exits = self.place_exit_orders(side, ep, qty_str)
                    return {
                        "filled": True,
                        "attempts": attempt,
                        "price": price,
                        "clientOrderId": cid,
                        "entryPrice": ep,
                        "exits": exits,
                        "mode": "maker",
                    }
                time.sleep(0.05)

            # Не успели — отменяем и пробуем дальше
            if placed:
                try:
                    self.client.cancel_order(self.symbol, orig_client_order_id=cid)
                except Exception:
                    pass
            time.sleep(self.order_timeout_ms / 1000)

        # --- Market фолбэк: добираем остаток ---
        step = float(self.step_size)
        remaining = self._remaining_to_target(side, float(qty_str))
        if remaining <= step / 2:
            ep = self.get_entry_price()
            exits = self.place_exit_orders(side, ep, qty_str)
            return {
                "filled": True,
                "attempts": int(market_fallback_after),
                "price": None,
                "clientOrderId": None,
                "entryPrice": ep,
                "exits": exits,
                "mode": "maker",
            }

        rem_str = round_to_step(remaining, self.step_size)
        try:
            bt = self.client.book_ticker(self.symbol)
            bid = float(bt["bidPrice"]); ask = float(bt["askPrice"])
            price_for_check = ask if side == "BUY" else bid
            rem_str = self._ensure_min_notional_qty(price_for_check, rem_str)
        except Exception:
            pass

        self.client.place_market(self.symbol, side, rem_str, reduce_only=False)
        amt, ep = self._wait_entry_info(timeout_ms=7000)
        if ep == 0.0:
            ep = self.get_entry_price()

        exits = self.place_exit_orders(side, ep, qty_str)
        return {
            "filled": True,
            "attempts": int(market_fallback_after),
            "price": None,
            "clientOrderId": None,
            "entryPrice": ep,
            "exits": exits,
            "mode": "market_fallback",
        }

    def _position_reached(self, side: Side, target_qty: float) -> bool:
        amt = float(self.get_position_amt())
        need = float(target_qty) * 0.999
        return (amt >= need) if side.upper() == "BUY" else (-amt >= need)

    def _remaining_to_target(self, side: Side, target_qty: float) -> float:
        """
        Сколько объёма ещё не добрали в текущую сторону (one-way режим).
        Предполагается, что встречная позиция уже закрыта выше по логике execute_signal().
        """
        amt = float(self.get_position_amt())
        current_same_dir = max(amt, 0.0) if side == "BUY" else max(-amt, 0.0)
        need = max(0.0, float(target_qty) - current_same_dir)
        return need


    def close_opposite_if_any(self, side: Side):
        """Сначала пробуем post-only reduceOnly, при -5022 — MARKET closePosition."""
        def remaining_qty() -> float:
            amt = self.get_position_amt()
            need_close = (side == "BUY" and amt < 0) or (side == "SELL" and amt > 0)
            return abs(amt) if need_close else 0.0

        rem = remaining_qty()
        if rem == 0.0:
            return {"closed": False, "info": "no opposite position"}

        attempts = 0
        step = float(self.step_size)

        while attempts < self.max_retries:
            rem = remaining_qty()
            if rem <= step / 2:
                return {"closed": True, "attempts": attempts, "info": "position flat"}

            attempts += 1
            close_side: Side = "BUY" if (side == "BUY") else "SELL"
            qty = round_to_step(rem, self.step_size)
            price = self.maker_price(close_side)
            cid = f"close-{uuid.uuid4().hex[:10]}"

            try:
                self.client.place_limit_post_only(
                    self.symbol, close_side, qty, price, reduce_only=True, new_client_order_id=cid
                )
            except ClientError as e:
                if getattr(e, "error_code", None) == -5022:
                    log.warning(f"[CLOSE] Post-only rejected (-5022). Fallback MARKET reduceOnly. side={close_side}, qty={qty}")
                    try:
                        self.client.place_market(self.symbol, close_side, qty, reduce_only=True, new_client_order_id=f"close-mkt-{uuid.uuid4().hex[:10]}")
                        deadline_mkt = self.client.now_ms() + int(self.close_timeout_ms * 0.5)
                        while self.client.now_ms() < deadline_mkt:
                            if remaining_qty() <= step / 2:
                                return {"closed": True, "attempts": attempts, "info": "position flat (market fallback)"}
                            time.sleep(0.05)
                        time.sleep(self.order_timeout_ms / 1000)
                        continue
                    except Exception:
                        time.sleep(self.order_timeout_ms / 1000)
                        continue
                time.sleep(self.order_timeout_ms / 1000)
                continue

            deadline = self.client.now_ms() + self.close_timeout_ms
            filled_enough = False
            while self.client.now_ms() < deadline:
                if remaining_qty() <= step / 2:
                    filled_enough = True
                    break
                time.sleep(0.05)

            if filled_enough:
                return {"closed": True, "attempts": attempts, "info": "position flat"}

            try:
                self.client.cancel_order(self.symbol, orig_client_order_id=cid)
            except Exception:
                pass

            time.sleep(self.order_timeout_ms / 1000)

        raise RuntimeError("Failed to close opposite position in time")

    def execute_signal(self, side_str: str, qty: float | None = None, spam_mode: bool = False):
        """
        Высокоуровневый вход:
        - чистим старые TP/SL,
        - закрываем встречную позицию,
        - при spam_mode идём сразу MARKET,
          иначе пробуем post-only с ограниченным числом попыток и фолбэком в MARKET.
        """
        s = side_str.lower()
        if s in ("long", "buy"):
            side: Side = "BUY"
        elif s in ("short", "sell"):
            side: Side = "SELL"
        else:
            raise ValueError(f"Unknown side: {side_str}")

        # убрать висящие TP/SL от прошлого входа
        self.cancel_exit_orders()

        # закрыть встречную позицию (мягко)
        try:
            self.close_opposite_if_any(side)
        except Exception:
            try:
                self.close_market(side)
            except Exception:
                pass

        # Реально переключаемся на MARKET в spam-режиме
        log.info(f"[OPEN] mode={'spam' if spam_mode else 'normal'}; strategy={'market' if spam_mode else 'postonly_then_market_fallback'}")
        if spam_mode:
            return self.open_market(side, qty=qty)
        else:
            return self.open_postonly_maker(side, qty=qty)

