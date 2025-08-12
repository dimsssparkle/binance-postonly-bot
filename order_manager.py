from __future__ import annotations
import time, uuid
from typing import Literal, Dict, Any
from binance.error import ClientError
from binance_client import BinanceFutures
from utils import round_to_step, parse_symbol_filters

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
        pr = self.client.position_risk(self.symbol)
        if not pr:
            return 0.0
        pos_amt = float(pr[0]["positionAmt"])  # >0 long, <0 short, 0 flat
        return pos_amt

    def close_opposite_if_any(self, side: Side):
        """
        Закрываем встречную позицию reduceOnly post-only.
        - Джойнимся к лучшей цене, чтобы поймать встречные рыночные агрессоры.
        - Учитываем частичное исполнение: после каждой попытки переизмеряем позицию
          и добираем остаток, пока не станет 0.
        """
        def remaining_qty() -> float:
            amt = self.get_position_amt()
            need_close = (side == "BUY" and amt < 0) or (side == "SELL" and amt > 0)
            return abs(amt) if need_close else 0.0

        # Быстрый выход, если закрывать нечего
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
            # Если нам нужно BUY для закрытия (то есть была шорт‑позиция), close_side=BUY; если открываем SELL — закрываем лонг SELL.
            # На самом деле совпадает с 'side' для открытия противоположного, т.к. мы сначала закрываем старую.

            qty = round_to_step(rem, self.step_size)
            price = self.maker_price(close_side)
            cid = f"close-{uuid.uuid4().hex[:10]}"

            try:
                self.client.place_limit_post_only(
                    self.symbol, close_side, qty, price, reduce_only=True, new_client_order_id=cid
                )
            except ClientError:
                # Например, "would be immediately match" — подождём и репрайснем
                time.sleep(self.order_timeout_ms / 1000)
                continue

            # Даём немного больше времени на fill при закрытии (x2 таймаута)
            deadline = self.client.now_ms() + self.close_timeout_ms
            filled_enough = False

            while self.client.now_ms() < deadline:
                # Периодически переизмеряем остаток позиции — это надёжнее, чем статус ордера (из‑за partial)
                if remaining_qty() <= step / 2:
                    filled_enough = True
                    break
                time.sleep(0.05)

            if filled_enough:
                return {"closed": True, "attempts": attempts, "info": "position flat"}

            # Иначе отменяем ордер и пробуем снова с новым прайсом
            try:
                self.client.cancel_order(self.symbol, orig_client_order_id=cid)
            except Exception:
                pass

            time.sleep(self.order_timeout_ms / 1000)

        raise RuntimeError("Failed to close opposite position in time")


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


    # def _has_position(self, side: str, target_qty: float) -> bool:
    #     """
    #     Проверяет, есть ли уже открытая позиция на нужной стороне и с объёмом >= target_qty.
    #     """
    #     try:
    #         positions = self.client.futures_position_information(symbol=self.symbol)
    #     except Exception as e:
    #         print(f"[ERROR] Failed to fetch positions: {e}")
    #         return False

    #     for pos in positions:
    #         amt = float(pos["positionAmt"])
    #         if amt == 0:
    #             continue
    #         # Лонг
    #         if side.upper() == "BUY" and amt > 0 and abs(amt) >= target_qty:
    #             return True
    #         # Шорт
    #         if side.upper() == "SELL" and amt < 0 and abs(amt) >= target_qty:
    #             return True
    #     return False



    # -------- High-level: flip if needed, then open --------
    def execute_signal(self, side_str: str, qty: float | None = None):
        side: Side = "BUY" if side_str.lower() == "long" else "SELL"
        # 1) Закрыть противоположную позицию, если есть
        self.close_opposite_if_any(side)
        # 2) Открыть новую post-only
        return self.open_postonly_maker(side, qty=qty)
