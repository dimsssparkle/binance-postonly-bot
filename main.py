from __future__ import annotations
import os
import logging
log = logging.getLogger(__name__)
log.info(f"API key length: {len(os.getenv('BINANCE_API_KEY',''))}, secret length: {len(os.getenv('BINANCE_API_SECRET',''))}")
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, Body
from pydantic import BaseModel, field_validator
from binance_client import BinanceFutures
from order_manager import OrderManager
from utils import parse_symbol_filters
from config import (
    SYMBOL_DEFAULT, QTY_DEFAULT, LEVERAGE_DEFAULT, ORDER_TIMEOUT_MS, MAX_RETRIES, CLOSE_TIMEOUT_MS,
    TV_WEBHOOK_SECRET, LOG_LEVEL, PORT, HEDGE_MODE
)
import uvicorn

logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger("app")

app = FastAPI(title="Binance Post-Only Bot", version="1.0.0")

client = BinanceFutures()
_exchange_info = client.exchange_info()  # кэшируем filters

class TVPayload(BaseModel):
    symbol: str | None = None
    side: str  # "long" | "short"
    secret: str | None = None

    @field_validator("side")
    @classmethod
    def validate_side(cls, v):
        v2 = v.lower()
        if v2 not in ("long", "short"):
            raise ValueError("side must be 'long' or 'short'")
        return v2

class ManualPayload(BaseModel):
    symbol: str | None = None
    side: str  # "long" | "short"
    qty: float | None = None

    @field_validator("side")
    @classmethod
    def validate_side(cls, v):
        v2 = v.lower()
        if v2 not in ("long", "short"):
            raise ValueError("side must be 'long' or 'short'")
        return v2

class ClosePayload(BaseModel):
    side: Optional[str] = None  # можно не передавать

def build_manager(symbol: str, qty_default: float) -> OrderManager:
    filters = parse_symbol_filters(_exchange_info, symbol)
    om = OrderManager(
        client=client,
        symbol=symbol,
        qty_default=qty_default,
        tick_size=filters["tickSize"],
        step_size=filters["stepSize"],
        order_timeout_ms=ORDER_TIMEOUT_MS,
        max_retries=MAX_RETRIES,
        close_timeout_ms=CLOSE_TIMEOUT_MS,
    )
    return om

@app.on_event("startup")
def _init():
    # Не вызываем приватные эндпоинты, чтобы сервер не падал при проблемах с ключами.
    log.info("Startup OK: skipping position mode/leverage at startup")


@app.get("/healthz")
def health():
    return {"ok": True}

@app.post("/tv/webhook")
def tv_webhook(payload: TVPayload):
    if TV_WEBHOOK_SECRET:
        if payload.secret != TV_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="bad secret")

    symbol = (payload.symbol or SYMBOL_DEFAULT).upper()
    # Гарантируем что плечо и маржинальность схлопнуты под символ
    try:
        client.set_margin_type_isolated(symbol)
    except Exception:
        pass
    try:
        client.set_leverage(symbol, LEVERAGE_DEFAULT)
    except Exception:
        pass

    om = build_manager(symbol, QTY_DEFAULT)
    try:
        result = om.execute_signal(payload.side)
        return {"status": "ok", "symbol": symbol, "action": payload.side, "result": result}
    except Exception as e:
        log.exception("execute_signal failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/trade/manual")
def manual_trade(payload: ManualPayload):
    symbol = (payload.symbol or SYMBOL_DEFAULT).upper()
    try:
        client.set_margin_type_isolated(symbol)
    except Exception:
        pass
    try:
        client.set_leverage(symbol, LEVERAGE_DEFAULT)
    except Exception:
        pass

    om = build_manager(symbol, payload.qty or QTY_DEFAULT)
    try:
        result = om.execute_signal(payload.side, qty=payload.qty)
        return {"status": "ok", "symbol": symbol, "action": payload.side, "result": result}
    except Exception as e:
        log.exception("manual execute_signal failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/trade/close")
def close_position(payload: Optional[ClosePayload] = None):
    """
    Закрывает текущую открытую позицию по SYMBOL_DEFAULT
    (или можно расширить на symbol из payload, если понадобится).
    """
    symbol = SYMBOL_DEFAULT.upper()
    om = build_manager(symbol, QTY_DEFAULT)

    try:
        pos_amt = om.get_position_amt()
        log.info(f"[CLOSE] позиция по {symbol}: {pos_amt}")
    except Exception as e:
        log.exception("Failed to get position amount")
        raise HTTPException(status_code=500, detail=str(e))

    if pos_amt == 0:
        return {"status": "ok", "symbol": symbol, "result": {"closed": False, "info": "already flat"}}

    # Если пользователь попросил закрыть только long/short — проверим соответствие
    if payload and payload.side:
        want = payload.side.lower()
        if want == "long" and pos_amt <= 0:
            return {"status": "ok", "symbol": symbol, "result": {"closed": False, "info": "no such position (not long)"}}
        if want == "short" and pos_amt >= 0:
            return {"status": "ok", "symbol": symbol, "result": {"closed": False, "info": "no such position (not short)"}}

    # Авто-сторона для закрытия: лонг -> SELL, шорт -> BUY
    side_for_close = "SELL" if pos_amt > 0 else "BUY"

    try:
        result = om.close_opposite_if_any(side_for_close)
        return {"status": "ok", "symbol": symbol, "result": result}
    except Exception as e:
        log.exception("close_position failed")
        raise HTTPException(status_code=500, detail=str(e))



if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
