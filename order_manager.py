from __future__ import annotations
import time, uuid
from typing import Literal, Dict, Any
from binance.error import ClientError
from binance_client import BinanceFutures
from utils import round_to_step, parse_symbol_filters
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

    # -------- Price helpers for maker placement --------
    def maker_price(self, side: Side) -> str:
        """
        BUY: становимся в best bid (join the touch).
        SELL: становимся в best ask.
        Если спред схлопнулся, отступаем на 1 tick, чтобы не пересечься и остаться мейкером.
        """
        bt = self.client.book_ticker(self.symbol)
        best_bid = float(bt["bidPrice"])
        best_ask = float(bt["askPrice"])
        tick = float(self.tick_size)

        if side == "BUY":
            target = best_bid
            if target >= best_ask:
                target = best_ask - tick
        else:  # side == "SELL"
            target = best_ask
            if target <= best_bid:
                target = best_bid + tick

        return round_to_step(target, self.tick_size)

    def norm_qty(self, qty: float | None) -> str:
        q = qty if qty is not None else self.qty_default
        return round_to_step(q, self.step_size)

    # -------- Opposite position handling --------
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

    def execute_signal(self, side_str: str, qty: float | None = None, spam_mode: bool = False):
        side: Side = "BUY" if side_str.lower() == "long" else "SELL"

        if spam_mode:
            # Быстро закрыть и открыть рыночным
            try:
                self.close_opposite_if_any(side)
            except Exception:
                try:
                    self.close_market(side)
                except Exception:
                    pass

            res = self.open_market(side, qty=qty)
            return res | {"mode": "market"}
        else:
            # Тихий режим: пост-онли
            self.close_opposite_if_any(side)
            res = self.open_postonly_maker(side, qty=qty)
            return res | {"mode": "post-only"}



    # -------- Market helpers (для шумных режимов) --------
    def close_market(self, side: Side):
        """
        Быстро закрыть встречную позицию рыночным reduceOnly.
        side=BUY закрывает шорт; side=SELL закрывает лонг.
        """
        rem = abs(self.get_position_amt())
        if rem <= 0:
            return {"closed": False, "info": "flat"}
        q = round_to_step(rem, self.step_size)
        self.client.place_market(self.symbol, side, q, reduce_only=True)
        return {"closed": True, "info": "market close", "qty": q}

    def open_market(self, side: Side, qty: float | None = None):
        q = self.norm_qty(qty)
        self.client.place_market(self.symbol, side, q, reduce_only=False)
        return {"filled": True, "price": None, "clientOrderId": None, "mode": "market"}


    # -------- Open maker order with rapid reprice loop --------
    def open_postonly_maker(self, side: Side, qty: float | None = None):
        """
        Открывает позицию лимитным Post-Only, репрайсит до исполнения.
        Останавливается, если позиция уже достигнута (по факту позиции, а не по статусу ордера).
        """
        q = self.norm_qty(qty)
        attempts = 0

        # Быстрый выход: позиция уже есть (например, предыдущая попытка успела исполниться)
        if self._position_reached(side, q):
            return {"filled": True, "attempts": 0, "price": None, "clientOrderId": None}

        while attempts < self.max_retries:
            attempts += 1
            price = self.maker_price(side)
            cid = f"open-{uuid.uuid4().hex[:10]}"

            # Ставим лимитный post-only
            try:
                self.client.place_limit_post_only(
                    self.symbol, side, q, price,
                    reduce_only=False,
                    new_client_order_id=cid
                )
            except Exception as e:
                # например, "would immediately match" — подождём и репрайснем
                time.sleep(self.order_timeout_ms / 1000)
                continue

            # Ждём исполнения, проверяя ФАКТ позиции (это надёжнее статуса ордера)
            deadline = self.client.now_ms() + (self.order_timeout_ms * 2)
            while self.client.now_ms() < deadline:
                if self._position_reached(side, q):
                    return {"filled": True, "attempts": attempts, "price": price, "clientOrderId": cid}
                time.sleep(0.05)

            # Не успели — отменяем и пробуем снова
            try:
                self.client.cancel_order(self.symbol, orig_client_order_id=cid)
            except Exception:
                pass

            # Контрольная проверка — вдруг позиция успела собраться в последнюю миллисекунду
            if self._position_reached(side, q):
                return {"filled": True, "attempts": attempts, "price": price, "clientOrderId": cid}

            time.sleep(self.order_timeout_ms / 1000)

        # Если сюда дошли — за max_retries так и не открылись
        raise RuntimeError("Failed to open maker order in time")

    def _position_reached(self, side: Side, target_qty: float) -> bool:
        """
        True, если позиция уже в нужную сторону и по модулю >= target_qty * 0.999.
        Long => положительный amt, Short => отрицательный.
        """
        amt = float(self.get_position_amt())
        need = float(target_qty) * 0.999
        if side.upper() == "BUY":
            return amt >= need
        else:  # "SELL"
            return -amt >= need

    def close_opposite_if_any(self, side: Side):
        """
        Закрываем встречную позицию reduceOnly post-only.
        Если пост-онли отклонён кодом -5022 (пересечение с рынком), мгновенно бьём MARKET reduceOnly.
        Учитываем частичное исполнение — после каждой попытки перепроверяем остаток позиции.
        """
        def remaining_qty() -> float:
            amt = self.get_position_amt()
            need_close = (side == "BUY" and amt < 0) or (side == "SELL" and amt > 0)
            return abs(amt) if need_close else 0.0

        # Быстрый выход: закрывать нечего
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
            close_side: Side = "BUY" if (side == "BUY") else "SELL"  # совпадает со стороной нового открытия
            qty = round_to_step(rem, self.step_size)
            price = self.maker_price(close_side)
            cid = f"close-{uuid.uuid4().hex[:10]}"

            try:
                # Пытаемся закрыть пост-онли, чтобы не платить таксу за агрессию
                self.client.place_limit_post_only(
                    self.symbol, close_side, qty, price, reduce_only=True, new_client_order_id=cid
                )
            except ClientError as e:
                # -5022: пост-онли отклонён (пересечение с рынком) — закрываемся маркетом, ждать нельзя
                if getattr(e, "error_code", None) == -5022:
                    log.warning(f"[CLOSE] Post-only rejected (-5022). Fallback MARKET reduceOnly. side={close_side}, qty={qty}")
                    mcid = f"close-mkt-{uuid.uuid4().hex[:10]}"
                    try:
                        self.client.place_market(
                            self.symbol, close_side, qty, reduce_only=True, new_client_order_id=mcid
                        )
                        deadline_mkt = self.client.now_ms() + int(self.close_timeout_ms * 0.5)
                        while self.client.now_ms() < deadline_mkt:
                            if remaining_qty() <= step / 2:
                                return {"closed": True, "attempts": attempts, "info": "position flat (market fallback)"}
                            time.sleep(0.05)
                        # если вдруг недосхлопнулось из-за шага — продолжим цикл
                        time.sleep(self.order_timeout_ms / 1000)
                        continue
                    except Exception:
                        # даже если маркет внезапно не прошёл, попробуем ещё цикл
                        time.sleep(self.order_timeout_ms / 1000)
                        continue

                # Любые другие ошибки — подождём и перепрайснем
                time.sleep(self.order_timeout_ms / 1000)
                continue

            # Если пост-онли поставился, ждём схлопывания позиции (проверяем по факту позиции)
            deadline = self.client.now_ms() + self.close_timeout_ms
            filled_enough = False
            while self.client.now_ms() < deadline:
                if remaining_qty() <= step / 2:
                    filled_enough = True
                    break
                time.sleep(0.05)

            if filled_enough:
                return {"closed": True, "attempts": attempts, "info": "position flat"}

            # Не успели — отменим ордер и перепрайсим
            try:
                self.client.cancel_order(self.symbol, orig_client_order_id=cid)
            except Exception:
                pass

            time.sleep(self.order_timeout_ms / 1000)

        raise RuntimeError("Failed to close opposite position in time")
