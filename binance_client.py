from __future__ import annotations
import time
from typing import Dict, Any, Optional
from binance.um_futures import UMFutures
from binance.error import ClientError
from config import API_KEY, API_SECRET, BASE_URL, LEVERAGE_DEFAULT, HEDGE_MODE

class BinanceFutures:
    def __init__(self):
        self.client = UMFutures(key=API_KEY, secret=API_SECRET, base_url=BASE_URL)

    # --- Meta / account ---
    def exchange_info(self) -> Dict[str, Any]:
        return self.client.exchange_info()

    def set_leverage(self, symbol: str, leverage: int = LEVERAGE_DEFAULT):
        return self.client.change_leverage(symbol=symbol, leverage=leverage)

    def set_margin_type_isolated(self, symbol: str):
        try:
            return self.client.change_margin_type(symbol=symbol, marginType="ISOLATED")
        except ClientError as e:
            # Если уже ISOLATED — вернёт ошибку -4046. Игнорируем.
            if getattr(e, "error_code", None) == -4046:
                return {"ignored": True}
            raise

    def set_position_mode(self, hedge: bool):
        return self.client.change_position_mode(dualSidePosition=hedge)

    # --- Order placement ---
    def place_limit_post_only(self, symbol: str, side: str, qty: str, price: str,
                              reduce_only: bool = False, new_client_order_id: Optional[str] = None):
        """
        side: BUY/SELL
        timeInForce=GTX — post-only
        """
        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="LIMIT",
            timeInForce="GTX",
            quantity=qty,
            price=price,
            reduceOnly=reduce_only,
            newClientOrderId=new_client_order_id
        )

    def cancel_order(self, symbol: str, order_id: int | None = None, orig_client_order_id: str | None = None):
        return self.client.cancel_order(symbol=symbol, orderId=order_id, origClientOrderId=orig_client_order_id)

    def get_order(self, symbol, order_id=None, orig_client_order_id=None):
        return self.client.query_order(
            symbol=symbol,
            orderId=order_id,
            origClientOrderId=orig_client_order_id
        )


    def book_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        Возвращает лучшую цену bid/ask — дешёвый эндпоинт для быстрой подстройки.
        """
        return self.client.book_ticker(symbol=symbol)

    # --- Positions / PnL ---
    def position_risk(self, symbol: str | None = None):
        data = self.client.get_position_risk()
        if symbol:
            data = [x for x in data if x["symbol"] == symbol]
        return data

    def get_positions(self, symbol: str):
        """
        Универсально получить позиции по символу.
        Пробуем разные имена методов у UMFutures,
        в крайнем случае читаем из account()['positions'].
        Возвращает список позиций (как минимум с полями symbol, positionAmt).
        """
        sym = symbol.upper()

        # 1) Пробуем прямые методы разных версий
        for name in ("position_risk", "position_information", "futures_position_information"):
            m = getattr(self.client, name, None)
            if callable(m):
                return m(symbol=sym)

        # 2) Фолбэк: берём все позиции из account()
        acc = getattr(self.client, "account", None)
        if callable(acc):
            data = acc() or {}
            pos = data.get("positions", [])
            # Вернём только нужный символ (в формате, схожем с position_risk)
            return [p for p in pos if str(p.get("symbol", "")).upper() == sym]

        # Если ничего не нашли — явно падаем, чтобы в логах было видно
        raise AttributeError("Positions endpoint not found on UMFutures (tried: position_risk, position_information, futures_position_information, account)")

    # --- Helpers ---
    @staticmethod
    def is_filled(status: str) -> bool:
        return status == "FILLED"

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)
