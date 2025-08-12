from __future__ import annotations
import os
import logging
log = logging.getLogger(__name__)
log.info(f"API key length: {len(os.getenv('BINANCE_API_KEY',''))}, secret length: {len(os.getenv('BINANCE_API_SECRET',''))}")

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, field_validator
from binance_client import BinanceFutures
from order_manager import OrderManager
from utils import parse_symbol_filters
from config import (
    SYMBOL_DEFAULT, QTY_DEFAULT, LEVERAGE_DEFAULT, ORDER_TIMEOUT_MS, MAX_RETRIES,
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

def build_manager(symbol: str, qty_default: float) -> OrderManager:
    filters = parse_symbol_filters(_exchange_info, symbol)
    om = OrderManager(
        client=client,
        symbol=symbol,
        qty_default=qty_default,
        tick_size=filters["tickSize"],
        step_size=filters["stepSize"],
        order_timeout_ms=ORDER_TIMEOUT_MS,
        max_retries=MAX_RETRIES
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
