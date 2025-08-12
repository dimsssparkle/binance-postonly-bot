from __future__ import annotations
import time, uuid
from typing import Literal, Dict, Any
from binance.error import ClientError
from binance_client import BinanceFutures
from utils import round_to_step, parse_symbol_filters

Side = Literal["BUY", "SELL"]

class OrderManager:
    def __init__(self, client: BinanceFutures, symbol: str, qty_default: float,
                 tick_size: str, step_size: str, order_timeout_ms: int, max_retries: int):
        self.client = client
        self.symbol = symbol
        self.qty_default = qty_default
        self.tick_size = tick_size
        self.step_size = step_size
        self.order_timeout_ms = order_timeout_ms
        self.max_retries = max_retries

    # -------- Price helpers for maker placement --------
    def maker_price(self, side: Side) -> str:
        """
        Для BUY — ставим чуть НИЖЕ лучшего ask (чтобы не пересечься) -> post-only гарантирует, что не исполнится как тейкер.
        Для SELL — ставим чуть ВЫШЕ лучшего bid.
        Используем округление по tickSize.
        """
        bt = self.client.book_ticker(self.symbol)
        best_bid = float(bt["bidPrice"])
        best_ask = float(bt["askPrice"])
        if side == "BUY":
            # на cent ниже ask
            target = best_ask - float(self.tick_size)
        else:
            target = best_bid + float(self.tick_size)
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
        pos_amt = self.get_position_amt()
        if pos_amt == 0.0:
            return {"closed": False, "info": "no opposite position"}
        # Если хотим BUY, а позиция отрицательная -> закрываем short. И наоборот.
        need_close = (side == "BUY" and pos_amt < 0) or (side == "SELL" and pos_amt > 0)
        if not need_close:
            return {"closed": False, "info": "no opposite side"}

        close_side: Side = "BUY" if pos_amt < 0 else "SELL"
        qty = round_to_step(abs(pos_amt), self.step_size)
        attempts = 0
        while attempts < self.max_retries:
            attempts += 1
            price = self.maker_price(close_side)
            cid = f"close-{uuid.uuid4().hex[:10]}"
            try:
                o = self.client.place_limit_post_only(self.symbol, close_side, qty, price, reduce_only=True, new_client_order_id=cid)
            except ClientError as e:
                # Если пост-онли отклонён из-за пересечения — просто пробуем другую цену на следующей итерации
                time.sleep(self.order_timeout_ms / 1000)
                continue
            # Ждём коротко, проверяем статус
            deadline = self.client.now_ms() + self.order_timeout_ms
            filled = False
            while self.client.now_ms() < deadline:
                od = self.client.get_order(self.symbol, orig_client_order_id=cid)
                if self.client.is_filled(od["status"]):
                    filled = True
                    break
                time.sleep(0.05)
            if filled:
                return {"closed": True, "attempts": attempts, "order": o}
            # Иначе отменяем и повторяем
            try:
                self.client.cancel_order(self.symbol, orig_client_order_id=cid)
            except ClientError:
                pass
            time.sleep(self.order_timeout_ms / 1000)
        raise RuntimeError("Failed to close opposite position in time")

    # -------- Open maker order with rapid reprice loop --------
    def open_postonly_maker(self, side: Side, qty: float | None = None):
        """Быстрая переустановка лимиток каждые ~order_timeout_ms до FILLED."""
        q = self.norm_qty(qty)
        attempts = 0
        while attempts < self.max_retries:
            attempts += 1
            price = self.maker_price(side)
            cid = f"open-{uuid.uuid4().hex[:10]}"
            try:
                self.client.place_limit_post_only(self.symbol, side, q, price, new_client_order_id=cid)
            except ClientError:
                # Скорее всего «Would be immediately match» — попробуем снова через 200 мс с новой ценой
                time.sleep(self.order_timeout_ms / 1000)
                continue

            # Короткое ожидание fill; если нет — отменяем и репрайсим
            deadline = self.client.now_ms() + self.order_timeout_ms
            filled = False
            while self.client.now_ms() < deadline:
                od = self.client.get_order(self.symbol, orig_client_order_id=cid)
                if self.client.is_filled(od["status"]):
                    filled = True
                    break
                time.sleep(0.05)
            if filled:
                return {"filled": True, "attempts": attempts, "price": price, "clientOrderId": cid}
            # Отменить и попробовать снова
            try:
                self.client.cancel_order(self.symbol, orig_client_order_id=cid)
            except Exception:
                pass
            time.sleep(self.order_timeout_ms / 1000)

        raise RuntimeError("Failed to open maker order in time")

    # -------- High-level: flip if needed, then open --------
    def execute_signal(self, side_str: str, qty: float | None = None):
        side: Side = "BUY" if side_str.lower() == "long" else "SELL"
        # 1) Закрыть противоположную позицию, если есть
        self.close_opposite_if_any(side)
        # 2) Открыть новую post-only
        return self.open_postonly_maker(side, qty=qty)
