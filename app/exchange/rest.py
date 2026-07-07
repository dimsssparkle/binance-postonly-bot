from __future__ import annotations
import time
from typing import Any, Dict, Optional

from binance.um_futures import UMFutures
from binance.error import ClientError

from app.config import API_KEY, API_SECRET, BASE_URL, LEVERAGE_DEFAULT
from app.exchange.errors import MARGIN_TYPE_ALREADY_SET, POSITION_MODE_NO_CHANGE, is_code


class BinanceRestClient:
    """Тонкая обёртка над binance-futures-connector (USD-M Futures REST)."""

    def __init__(self) -> None:
        self.client = UMFutures(key=API_KEY, secret=API_SECRET, base_url=BASE_URL)

    # --- Meta / account setup ---
    def exchange_info(self) -> Dict[str, Any]:
        return self.client.exchange_info()

    def set_leverage(self, symbol: str, leverage: int = LEVERAGE_DEFAULT):
        return self.client.change_leverage(symbol=symbol, leverage=leverage)

    def set_margin_type_isolated(self, symbol: str):
        try:
            return self.client.change_margin_type(symbol=symbol, marginType="ISOLATED")
        except ClientError as e:
            if is_code(e, MARGIN_TYPE_ALREADY_SET):
                return {"ignored": True}
            raise

    def set_position_mode(self, hedge: bool):
        try:
            return self.client.change_position_mode(dualSidePosition=hedge)
        except ClientError as e:
            if is_code(e, POSITION_MODE_NO_CHANGE):
                return {"ignored": True, "reason": "already set"}
            raise

    # --- Order placement ---
    def place_limit_post_only(self, symbol: str, side: str, qty: str, price: str,
                               reduce_only: bool = False, new_client_order_id: Optional[str] = None):
        return self.client.new_order(
            symbol=symbol, side=side, type="LIMIT", timeInForce="GTX",
            quantity=qty, price=price, reduceOnly=reduce_only,
            newClientOrderId=new_client_order_id,
        )

    def place_market(self, symbol: str, side: str, qty: str,
                      reduce_only: bool = False, new_client_order_id: Optional[str] = None):
        return self.client.new_order(
            symbol=symbol, side=side, type="MARKET", quantity=qty,
            reduceOnly=reduce_only, newClientOrderId=new_client_order_id,
        )

    def place_take_profit_market(self, symbol: str, side: str, stop_price: str,
                                  new_client_order_id: Optional[str] = None):
        return self.client.new_order(
            symbol=symbol, side=side, type="TAKE_PROFIT_MARKET",
            stopPrice=stop_price, closePosition=True,
            newClientOrderId=new_client_order_id,
        )

    def place_stop_market(self, symbol: str, side: str, stop_price: str,
                           new_client_order_id: Optional[str] = None):
        return self.client.new_order(
            symbol=symbol, side=side, type="STOP_MARKET",
            stopPrice=stop_price, closePosition=True,
            newClientOrderId=new_client_order_id,
        )

    def cancel_order(self, symbol: str, order_id: int | None = None, orig_client_order_id: str | None = None):
        return self.client.cancel_order(symbol=symbol, orderId=order_id, origClientOrderId=orig_client_order_id)

    def cancel_all_open_orders(self, symbol: str):
        return self.client.cancel_open_orders(symbol=symbol)

    def get_order(self, symbol: str, order_id: int | None = None, orig_client_order_id: str | None = None):
        return self.client.query_order(symbol=symbol, orderId=order_id, origClientOrderId=orig_client_order_id)

    def list_open_orders(self, symbol: str):
        return self.client.get_open_orders(symbol=symbol.upper()) or []

    def book_ticker(self, symbol: str) -> Dict[str, Any]:
        return self.client.book_ticker(symbol=symbol)

    # --- Positions / account ---
    def get_position_risk(self, symbol: str | None = None):
        data = self.client.get_position_risk()
        if symbol:
            sym = symbol.upper()
            data = [p for p in data if str(p.get("symbol", "")).upper() == sym]
        return data

    def account_info(self) -> Dict[str, Any]:
        return self.client.account()

    def user_trades(self, symbol: str, startTime: int | None = None, endTime: int | None = None, limit: int = 1000):
        return self.client.get_account_trades(symbol=symbol, startTime=startTime, endTime=endTime, limit=limit)

    # --- User Data Stream (listenKey) ---
    def new_listen_key(self) -> str:
        resp = self.client.new_listen_key()
        return resp["listenKey"]

    def renew_listen_key(self, listen_key: str) -> None:
        self.client.renew_listen_key(listenKey=listen_key)

    def close_listen_key(self, listen_key: str) -> None:
        self.client.close_listen_key(listenKey=listen_key)

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)
