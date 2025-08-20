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
        try:
            return self.client.change_position_mode(dualSidePosition=hedge)
        except ClientError as e:
            # -4059 = "No need to change position side." — режим уже установлен
            if getattr(e, "error_code", None) == -4059:
                return {"ignored": True, "reason": "already set"}
            raise



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
        Кросс-совместимая выборка открытых ордеров по символу.
        Пробуем по приоритету:
          1) open_orders(symbol=...)
          2) get_open_orders(symbol=...)
          3) get_open_orders() -> фильтр по symbol
        В случае несоответствия сигнатуры/ошибок — тихо фолбэчим.
        """
        sym = symbol.upper()

        # Вариант 1: open_orders(symbol=...)
        m1 = getattr(self.client, "open_orders", None)
        if callable(m1):
            try:
                return m1(symbol=sym) or []
            except Exception:
                pass

        # Вариант 2: get_open_orders(symbol=...)
        m2 = getattr(self.client, "get_open_orders", None)
        if callable(m2):
            try:
                return m2(symbol=sym) or []
            except TypeError:
                # у некоторых сборок метод без параметров — фильтруем вручную
                try:
                    data = m2() or []
                    return [o for o in data if str(o.get("symbol","")).upper() == sym]
                except Exception:
                    pass
            except Exception:
                pass

        # Вариант 3: ничего не сработало — пусто
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


    def get_position_overview(self, symbol: str) -> Dict[str, Any]:
        """
        Снэпшот позиции для блока Position — только нужные поля:
          - symbol
          - side ("LONG"/"SHORT" или "")
          - positionAmt
          - entryPrice
          - markPrice
          - unRealizedProfit
          - leverage              (если не удаётся прочитать — падение на LEVERAGE_DEFAULT)
          - isolatedWallet
          - liquidationPrice
          - positionMargin        (единое поле вместо positionInitialMargin/openOrderInitialMargin/maintMargin)
        """
        from config import LEVERAGE_DEFAULT  # локальный импорт, чтобы избежать циклов

        sym = symbol.upper()

        snap: Dict[str, Any] = {
            "symbol": sym,
            "positionAmt": "0",
            "entryPrice": "0",
            "markPrice": "0",
            "unRealizedProfit": "0",
            "leverage": "0",
            "isolatedWallet": "0",
            "liquidationPrice": "0",
            # внутренние поля для расчёта единой маржи
            "_positionInitialMargin": "0",
            "_openOrderInitialMargin": "0",
            "_maintMargin": "0",
        }

        # 1) /account -> positions (даёт leverage, isolatedWallet, initialMargins)
        try:
            acc = self.client.account() or {}
            for p in acc.get("positions", []):
                if str(p.get("symbol", "")).upper() == sym:
                    def S(k, default="0"):
                        v = p.get(k)
                        return str(v) if v is not None else default

                    snap.update({
                        "positionAmt":                 S("positionAmt",                 snap["positionAmt"]),
                        "entryPrice":                  S("entryPrice",                  snap["entryPrice"]),
                        "unRealizedProfit":            S("unrealizedProfit",            snap["unRealizedProfit"]),  # из /account
                        "leverage":                    S("leverage",                    snap["leverage"]),
                        "isolatedWallet":              S("isolatedWallet",              snap["isolatedWallet"]),
                        "_positionInitialMargin":      S("positionInitialMargin",       snap["_positionInitialMargin"]),
                        "_openOrderInitialMargin":     S("openOrderInitialMargin",      snap["_openOrderInitialMargin"]),
                    })
                    break
        except Exception:
            pass

        # 2) /positionRisk (markPrice, liquidationPrice, maintMargin и иногда точнее entry/amt/unRealizedProfit/leverage)
        try:
            gpr = getattr(self.client, "get_position_risk", None)
            if callable(gpr):
                for r in gpr() or []:
                    if str(r.get("symbol", "")).upper() == sym:
                        def SR(k, default="0"):
                            v = r.get(k)
                            return str(v) if v is not None else default

                        upd = {
                            "markPrice":        SR("markPrice",        snap["markPrice"]),
                            "liquidationPrice": SR("liquidationPrice", snap["liquidationPrice"]),
                            "_maintMargin":     SR("maintMargin",      snap["_maintMargin"]),
                            "unRealizedProfit": SR("unRealizedProfit", snap["unRealizedProfit"]),
                            "positionAmt":      SR("positionAmt",      snap["positionAmt"]),
                            "entryPrice":       SR("entryPrice",       snap["entryPrice"]),
                            "leverage":         SR("leverage",         snap["leverage"]),
                        }
                        for k, v in upd.items():
                            if str(v) not in ("", "0", "0.0", "0.00"):
                                snap[k] = v
                        break
        except Exception:
            pass

        # 3) side по знаку позиции
        try:
            amt = float(snap.get("positionAmt", "0") or 0)
            side = "LONG" if amt > 0 else ("SHORT" if amt < 0 else "")
        except Exception:
            side = ""

        # 4) leverage fallback — если всё ещё "0", подставляем LEVERAGE_DEFAULT
        lev = str(snap.get("leverage", "0") or "0")
        if lev in ("", "0", "0.0", "0.00"):
            try:
                # попытка прочитать через наш helper
                alt = str(self.get_symbol_leverage(sym) or "")
                if alt not in ("", "0", "0.0", "0.00"):
                    lev = alt
                else:
                    lev = str(LEVERAGE_DEFAULT)
            except Exception:
                lev = str(LEVERAGE_DEFAULT)
        snap["leverage"] = lev

        # 5) Единое поле маржи позиции: берём maintMargin, если >0, иначе positionInitialMargin, иначе openOrderInitialMargin
        try:
            mm  = float(snap.get("_maintMargin", "0") or 0)
            pim = float(snap.get("_positionInitialMargin", "0") or 0)
            oim = float(snap.get("_openOrderInitialMargin", "0") or 0)
            if mm > 0:
                position_margin = mm
            elif pim > 0:
                position_margin = pim
            else:
                position_margin = oim
            position_margin_str = f"{position_margin:.10f}".rstrip('0').rstrip('.')
        except Exception:
            position_margin_str = "0"

        # 6) Очищенный ответ
        return {
            "symbol": snap["symbol"],
            "side": side,
            "positionAmt": snap["positionAmt"],
            "entryPrice": snap["entryPrice"],
            "markPrice": snap["markPrice"],
            "unRealizedProfit": snap["unRealizedProfit"],
            "leverage": snap["leverage"],
            "isolatedWallet": snap["isolatedWallet"],
            "liquidationPrice": snap["liquidationPrice"],
            "positionMargin": position_margin_str,
        }



    # --- Helpers ---
    @staticmethod
    def is_filled(status: str) -> bool:
        return status == "FILLED"

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)


    def account_info(self) -> Dict[str, Any]:
        """Сырые данные /fapi/v2/account."""
        return self.client.account()

    def get_symbol_leverage(self, symbol: str) -> str:
        """Левередж читаем сначала из /account (чаще верный даже без открытой позиции), затем из position_risk."""
        sym = symbol.upper()
        # /fapi/v2/account -> positions (приоритетнее)
        try:
            acc = self.client.account()
            for pp in acc.get("positions", []):
                if str(pp.get("symbol","")).upper() == sym:
                    lv = str(pp.get("leverage", "") or "")
                    if lv not in ("", "0", "0.0", "0.00"):
                        return lv
        except Exception:
            pass
        # position_risk
        try:
            for p in self.get_positions(sym) or []:
                if str(p.get("symbol","")).upper() == sym:
                    lv = str(p.get("leverage", "") or "")
                    if lv not in ("", "0", "0.0", "0.00"):
                        return lv
        except Exception:
            pass
        return "0"


    def get_symbol_margin_type(self, symbol: str) -> str:
        """Возвращает ISOLATED/CROSSED, если доступно."""
        sym = symbol.upper()
        try:
            acc = self.client.account()
            for pp in acc.get("positions", []):
                if str(pp.get("symbol","")).upper() == sym:
                    mt = (pp.get("marginType") or "").upper()
                    if mt:
                        return mt
        except Exception:
            pass
        return ""

    def user_trades(self, symbol: str, startTime: int | None = None, endTime: int | None = None, limit: int = 1000):
        """
        История сделок по символу (USD-M futures).
        Поля (по API Binance): symbol, orderId, side, price, qty, realizedPnl, commission,
        commissionAsset, time, buyer, maker, positionSide и т.д.
        """
        return self.client.get_account_trades(symbol=symbol, startTime=startTime, endTime=endTime, limit=limit)