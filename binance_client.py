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
            if getattr(e, "error_code", None) == -4046:
                return {"ignored": True}
            raise

    def set_position_mode(self, hedge: bool):
        return self.client.change_position_mode(dualSidePosition=hedge)

    # --- Order placement ---
    def place_limit_post_only(self, symbol: str, side: str, qty: str, price: str,
                              reduce_only: bool = False, new_client_order_id: Optional[str] = None):
        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="LIMIT",
            timeInForce="GTX",   # post-only
            quantity=qty,
            price=price,
            reduceOnly=reduce_only,
            newClientOrderId=new_client_order_id
        )

    def place_market(self, symbol: str, side: str, qty: str,
                     reduce_only: bool = False, new_client_order_id: Optional[str] = None):
        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty,
            reduceOnly=reduce_only,
            newClientOrderId=new_client_order_id
        )

    # ---- TP/SL (market) с closePosition=True ----
    def place_take_profit_market(self, symbol: str, side: str, stop_price: str,
                                 new_client_order_id: Optional[str] = None):
        # NB: quantity НЕ указываем, используем closePosition=True
        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=stop_price,
            closePosition=True,
            newClientOrderId=new_client_order_id
        )

    def place_stop_market(self, symbol: str, side: str, stop_price: str,
                          new_client_order_id: Optional[str] = None):
        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            stopPrice=stop_price,
            closePosition=True,
            newClientOrderId=new_client_order_id
        )

    def cancel_order(self, symbol: str, order_id: int | None = None, orig_client_order_id: str | None = None):
        return self.client.cancel_order(symbol=symbol, orderId=order_id, origClientOrderId=orig_client_order_id)

    def cancel_all_open_orders(self, symbol: str):
        """
        Обертка над DELETE /fapi/v1/allOpenOrders
        """
        return self.client.cancel_open_orders(symbol=symbol)


    def get_order(self, symbol, order_id=None, orig_client_order_id=None):
        return self.client.query_order(
            symbol=symbol,
            orderId=order_id,
            origClientOrderId=orig_client_order_id
        )

    def list_open_orders(self, symbol: str):
        """
        Возвращает открытые ордера по символу, если метод SDK доступен.
        Используется реже, т.к. для очистки мы теперь зовём cancel_open_orders().
        """
        m = getattr(self.client, "get_open_orders", None)
        if callable(m):
            try:
                return m(symbol=symbol)
            except TypeError:
                data = m() or []
                return [o for o in data if str(o.get("symbol","")).upper() == symbol.upper()]
        # если метода нет — вернём пусто (чтобы вызывающий код не падал)
        return []



    def book_ticker(self, symbol: str) -> Dict[str, Any]:
        return self.client.book_ticker(symbol=symbol)

    # --- Positions / PnL ---
    def position_risk(self, symbol: str | None = None):
        data = self.client.get_position_risk()
        if symbol:
            data = [x for x in data if x["symbol"] == symbol]
        return data

    def get_positions(self, symbol: str):
        """
        Сначала читаем get_position_risk() и фильтруем, т.к. это самый стабильный путь.
        Дальше — совместимость со старыми именами.
        """
        sym = symbol.upper()

        # 0) надёжный способ: get_position_risk() без символа
        gpr = getattr(self.client, "get_position_risk", None)
        if callable(gpr):
            data = gpr() or []
            return [p for p in data if str(p.get("symbol","")).upper() == sym]

        # 1) некоторые версии принимают symbol
        for name in ("position_risk", "position_information", "futures_position_information"):
            m = getattr(self.client, name, None)
            if callable(m):
                try:
                    return m(symbol=sym)
                except TypeError:
                    # без символа -> фильтруем вручную
                    data = m() or []
                    return [p for p in data if str(p.get("symbol","")).upper() == sym]

        # 2) фолбэк: account()['positions']
        acc = getattr(self.client, "account", None)
        if callable(acc):
            data = acc() or {}
            pos = data.get("positions", [])
            return [p for p in pos if str(p.get("symbol", "")).upper() == sym]

        raise AttributeError("Positions endpoint not found on UMFutures")


    # --- Helpers ---
    @staticmethod
    def is_filled(status: str) -> bool:
        return status == "FILLED"

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)
