import os
import logging
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
import time
import uvicorn
import json
from signal_router import SignalRouter

# Настроим логирование до первого использования логгера
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
log.debug(f"API key length: {len(os.getenv('BINANCE_API_KEY',''))}, secret length: {len(os.getenv('BINANCE_API_SECRET',''))}")

app = FastAPI(title="Binance Post-Only Bot", version="1.0.0")

client = BinanceFutures()
_exchange_info = client.exchange_info()  # кэшируем filters

from threading import Lock
_locks: dict[str, Lock] = {}

def _lock_for(symbol: str) -> Lock:
    s = symbol.upper()
    lk = _locks.get(s)
    if lk is None:
        lk = Lock()
        _locks[s] = lk
    return lk


router = SignalRouter(
    W=int(os.getenv("SPAM_WINDOW_SEC", 90)),
    N=int(os.getenv("SPAM_COUNT", 4)),
    F=int(os.getenv("SPAM_FLIPS", 3)),
    T_hold=int(os.getenv("SPAM_MIN_HOLD_SEC", 30)),
    H=int(os.getenv("SPAM_HYSTERESIS_SEC", 60)),
)

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

def ensure_symbol_setup(symbol: str) -> None:
    try:
        client.set_margin_type_isolated(symbol)
    except Exception:
        pass
    try:
        client.set_leverage(symbol, LEVERAGE_DEFAULT)
    except Exception:
        pass

@app.post("/webhook")
async def tv_webhook(request: Request, secret: Optional[str] = None):
    # Читаем тело ОДИН раз (для JSON и fallback)
    raw = await request.body()
    payload = {}

    # Пытаемся распарсить JSON
    try:
        if raw:
            payload = json.loads(raw.decode("utf-8"))
    except Exception:
        # fallback: text/plain вида "key: val"
        try:
            text = raw.decode("utf-8", errors="ignore")
            for line in text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    payload[k.strip()] = v.strip()
        except Exception:
            payload = {}

    # Секьюрность: принимаем секрет из query, заголовка, либо тела
    header_secret = request.headers.get("X-Webhook-Secret")
    body_secret = (payload.get("secret") or None) if isinstance(payload, dict) else None
    if TV_WEBHOOK_SECRET:
        provided = secret or header_secret or body_secret
        if provided != TV_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="bad secret")

    # Нормализуем вход
    side = (payload.get("side") or "").lower()
    symbol = (payload.get("symbol") or SYMBOL_DEFAULT).upper()
    if side not in ("long", "short"):
        raise HTTPException(status_code=400, detail="side must be 'long' or 'short'")

    lk = _lock_for(symbol)
    with lk:
        ensure_symbol_setup(symbol)

        router.register(side)
        spam = router.in_spam()
        if spam:
            log.info("[SPAM MODE] reason=frequency_or_hold | "
                     f"events_in_window={len(router.events)}; "
                     f"flips={sum(1 for i in range(1, len(router.events)) if router.events[i-1][1] != router.events[i][1])}; "
                     f"since_last_open={int(time.time() - router.last_open_ts) if router.last_open_ts else 'n/a'}s")


        om = build_manager(symbol, QTY_DEFAULT)
        try:
            result = om.execute_signal(side, spam_mode=spam)
            if result.get("filled"):
                router.start_opened()
            return {
                "status": "ok",
                "symbol": symbol,
                "action": side,
                "mode": result.get("mode"),
                "result": result
            }
        except Exception as e:
            log.exception("execute_signal failed")
            raise HTTPException(status_code=500, detail=str(e))


@app.on_event("startup")
def _init():
    # Не вызываем критичные приватные эндпоинты без защиты
    log.info("Startup OK: applying safe initial settings")
    try:
        client.set_position_mode(hedge=(HEDGE_MODE.lower() == "on"))
        log.info(f"Position mode set: {'HEDGE' if HEDGE_MODE.lower() == 'on' else 'ONE-WAY'}")
    except Exception:
        log.warning("Failed to set position mode; will continue without changing it", exc_info=True)



@app.get("/healthz")
def health():
    return {"ok": True}


@app.post("/trade/manual")
def manual_trade(payload: ManualPayload):
    symbol = (payload.symbol or SYMBOL_DEFAULT).upper()
    side = payload.side

    lk = _lock_for(symbol)
    with lk:
        ensure_symbol_setup(symbol)

        router.register(side)
        spam = router.in_spam()
        if spam:
            log.info("[SPAM MODE] reason=frequency_or_hold | "
                     f"events_in_window={len(router.events)}; "
                     f"flips={sum(1 for i in range(1, len(router.events)) if router.events[i-1][1] != router.events[i][1])}; "
                     f"since_last_open={int(time.time() - router.last_open_ts) if router.last_open_ts else 'n/a'}s")


        om = build_manager(symbol, payload.qty or QTY_DEFAULT)
        try:
            result = om.execute_signal(side, qty=payload.qty, spam_mode=spam)
            if result.get("filled"):
                router.start_opened()
            return {
                "status": "ok",
                "symbol": symbol,
                "action": side,
                "mode": result.get("mode"),
                "result": result
            }
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
