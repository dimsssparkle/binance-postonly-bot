from __future__ import annotations
import time, uuid
from typing import Literal, Dict, Any
from binance.error import ClientError
from binance_client import BinanceFutures
from utils import round_to_step
from config import TP_PCT, SL_PCT  # <-- берем TP/SL из одного источника
import logging

log = logging.getLogger("order_manager")
Side = Literal["BUY", "SELL"]

class OrderManager:
    def __init__(self, client: BinanceFutures, symbol: str, qty_default: float,
                 tick_size: str, step_size: str, order_timeout_ms: int, max_retries: int,
                 close_timeout_ms: int | None = None):
        self.client = client
        self.symbol = symbol
        self.qty_default = qty_default
        self.tick_size = tick_size
        self.step_size = step_size
        self.order_timeout_ms = order_timeout_ms
        self.max_retries = max_retries
        self.close_timeout_ms = close_timeout_ms or (self.order_timeout_ms * 2)

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

        # если оба выключены — быстро выходим
        if tp_pct <= 0 and sl_pct <= 0:
            return placed

        # расчёт цен триггеров
        if entry_price > 0:
            if side == "BUY":  # long
                tp_price = entry_price * (1.0 + tp_pct) if tp_pct > 0 else None
                sl_price = entry_price * (1.0 - sl_pct) if sl_pct > 0 else None
            else:              # short
                tp_price = entry_price * (1.0 - tp_pct) if tp_pct > 0 else None
                sl_price = entry_price * (1.0 + sl_pct) if sl_pct > 0 else None
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
        Увеличили таймаут до 5с — у Binance обновление entryPrice иногда запаздывает.
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

        # 1) открыть market
        self.client.place_market(self.symbol, side, qty_str, reduce_only=False)

        # 2) получить entryPrice из позиций (резерв — mid по стакану внутри place_exit_orders)
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


    # -------- Open maker order with rapid reprice loop --------
    def open_postonly_maker(self, side: Side, qty: float | None = None):
        """
        Открывает позицию лимитным Post-Only, репрайсит до исполнения.
        После успешного входа выставляет TP/SL.
        """
        qty_str = self.norm_qty(qty)
        attempts = 0

        # Если позиция уже достигнута по факту — просто поставим выходы и вернёмся
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

        while attempts < self.max_retries:
            attempts += 1
            price = self.maker_price(side)
            cid = f"open-{uuid.uuid4().hex[:10]}"

            # Ставим лимитный post-only
            try:
                self.client.place_limit_post_only(
                    self.symbol, side, qty_str, price,
                    reduce_only=False,
                    new_client_order_id=cid
                )
            except Exception:
                # например, -5022 (иммедиат матч) — ждём и репрайсим
                time.sleep(self.order_timeout_ms / 1000)
                continue

            # Ждём исполнения по факту позиции (частичное/полное)
            deadline = self.client.now_ms() + (self.order_timeout_ms * 2)
            while self.client.now_ms() < deadline:
                if self._position_reached(side, float(qty_str)):
                    ep = self.get_entry_price()
                    exits = self.place_exit_orders(side, ep, qty_str)
                    return {
                        "filled": True,
                        "attempts": attempts,
                        "price": price,
                        "clientOrderId": cid,
                        "entryPrice": ep,
                        "exits": exits,
                        "mode": "maker",
                    }
                time.sleep(0.05)

            # Не успели — отменяем и пробуем снова
            try:
                self.client.cancel_order(self.symbol, orig_client_order_id=cid)
            except Exception:
                pass

            # На всякий случай: вдруг долилось в последнюю миллисекунду
            if self._position_reached(side, float(qty_str)):
                ep = self.get_entry_price()
                exits = self.place_exit_orders(side, ep, qty_str)
                return {
                    "filled": True,
                    "attempts": attempts,
                    "price": price,
                    "clientOrderId": cid,
                    "entryPrice": ep,
                    "exits": exits,
                    "mode": "maker",
                }

            time.sleep(self.order_timeout_ms / 1000)

        raise RuntimeError("Failed to open maker order in time")


    def _position_reached(self, side: Side, target_qty: float) -> bool:
        amt = float(self.get_position_amt())
        need = float(target_qty) * 0.999
        return (amt >= need) if side.upper() == "BUY" else (-amt >= need)

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
        - открываем новую (market в шуме, post-only в нормальном режиме),
        - (open_market/open_postonly_maker сами поставят TP/SL).
        side_str: "long"/"short" (или "buy"/"sell")
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

        if spam_mode:
            try:
                self.close_opposite_if_any(side)
            except Exception:
                try:
                    self.close_market(side)
                except Exception:
                    pass
            return self.open_market(side, qty=qty)
        else:
            self.close_opposite_if_any(side)
            return self.open_postonly_maker(side, qty=qty)
