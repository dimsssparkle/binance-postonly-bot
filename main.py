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
    TV_WEBHOOK_SECRET, LOG_LEVEL, PORT, HEDGE_MODE, TP_PCT, SL_PCT
)
import time
import uvicorn
import json
from signal_router import SignalRouter

from starlette.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse, HTMLResponse
import asyncio

from threading import Lock

# Настроим логирование до первого использования логгера
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
log.debug(f"API key length: {len(os.getenv('BINANCE_API_KEY',''))}, secret length: {len(os.getenv('BINANCE_API_SECRET',''))}")

app = FastAPI(title="Binance Post-Only Bot", version="1.0.0")

client = BinanceFutures()
_exchange_info = client.exchange_info()  # кэшируем filters

# runtime-настройки TP/SL (по умолчанию включены, если проценты > 0)
_runtime_opts = {
    "tp_enabled": (float(TP_PCT or 0.0) > 0.0),
    "sl_enabled": (float(SL_PCT or 0.0) > 0.0),
}

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

class TpSlSettingsPayload(BaseModel):
    tp_enabled: Optional[bool] = None
    sl_enabled: Optional[bool] = None
    symbol: Optional[str] = None  # для применения "отключения" к конкретному символу при очистке выходов


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
        tp_enabled=_runtime_opts["tp_enabled"],
        sl_enabled=_runtime_opts["sl_enabled"],
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
        resp = client.set_position_mode(hedge=(HEDGE_MODE.lower() == "on"))
        if isinstance(resp, dict) and resp.get("ignored"):
            log.info("Position mode already set; skipping change")
        else:
            log.info(f"Position mode set: {'HEDGE' if HEDGE_MODE.lower() == 'on' else 'ONE-WAY'}")
    except Exception as e:
        log.warning(f"Failed to set position mode: {e}")





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

def _orders_snapshot(symbol: str):
    sym = symbol.upper()

    # Позиция (расширенный снэпшот)
    try:
        position = client.get_position_overview(sym)
    except Exception as e:
        log.warning(f"[ORDERS] failed to build position overview: {e}", exc_info=True)
        position = {
            "symbol": sym,
            "positionAmt": "0",
            "entryPrice": "0",
            "markPrice": "0",
            "unRealizedProfit": "0",
            "leverage": "0",
            "marginType": "",
        }

    # Стакан (best bid/ask)
    book = {}
    try:
        bt = client.book_ticker(sym) or {}
        book = {"bidPrice": bt.get("bidPrice"), "askPrice": bt.get("askPrice")}
    except Exception as e:
        log.warning(f"[ORDERS] failed to fetch book_ticker: {e}", exc_info=True)

    return {
        "symbol": sym,
        "position": position,
        "book": book,
        "ts": int(time.time() * 1000),
    }


def _roundtrips_from_trades(symbol: str, limit_rounds: int = 50, limit_trades: int = 1000):
    """
    Восстанавливает «закрытые позиции» (раунды) из последовательности трейдов.
    Логика:
      - накапливаем позицию pos (BUY=+qty, SELL=-qty) в one-way режиме;
      - старт раунда — когда из 0 уходим в ненулевую позицию;
      - закрытие раунда — когда позиция возвращается к 0 (или трейд «перестрелил» и часть ушла в новый раунд);
      - комиссии: часть комиссии трейда, закрывающая позицию, идёт в fee_exit; оставшаяся — в fee_entry (если был «перестрёл» — в новый раунд);
      - realizedPnl берём из поля трейда и учитываем только на редьюсе (у Binance оно как раз на закрывающей части).
    Примечание: если один трейд частично закрывает и тут же открывает в обратную сторону, комиссия сплитится пропорционально.
    """
    trades = client.user_trades(symbol, limit=limit_trades) or []
    trades = sorted(trades, key=lambda t: int(t.get("time", 0)))

    rounds = []
    pos = 0.0                 # текущая позиция (со знаком)
    direction = 0             # +1 лонг, -1 шорт; 0 — плоско
    current = None            # текущий раунд

    for t in trades:
        side = (t.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        qty = float(t.get("qty") or t.get("quantity") or 0.0)
        if qty <= 0:
            continue

        commission = abs(float(t.get("commission") or 0.0))
        realized = float(t.get("realizedPnl") or 0.0)
        ts = int(t.get("time") or 0)

        sign = 1 if side == "BUY" else -1
        val = sign * qty

        same_dir_or_flat = (pos == 0) or (pos * sign > 0)
        if same_dir_or_flat:
            # чистый набор (entry)
            if current is None:
                direction = sign
                current = {
                    "symbol": symbol,
                    "side": "LONG" if direction == 1 else "SHORT",
                    "qty": 0.0,
                    "fee_entry": 0.0,
                    "fee_exit": 0.0,
                    "fee_total": 0.0,
                    "pnl_realized": 0.0,
                    "pnl_net": 0.0,
                    "open_time": ts,
                    "close_time": None,
                }
            current["qty"] += qty
            current["fee_entry"] += commission
            pos += val
            continue

        # Иначе — редьюс текущей позиции (возможен «перестрёл»)
        reduce_amt = min(abs(pos), qty)       # часть, закрывающая старую позицию
        overshoot = qty - reduce_amt          # остаток, который уйдет в новый раунд
        exit_fee = commission * (reduce_amt / qty) if qty > 0 else 0.0
        entry_fee_for_overshoot = commission - exit_fee

        # Реализованный PnL у Binance уже относится к редьюс-части — не скейлим
        if current is not None:
            current["fee_exit"] += exit_fee
            current["pnl_realized"] += realized

        # уменьшаем позицию
        # редьюс всегда движет pos к 0, т.е. просто вычитаем модуль reduce_amt с соответствующим знаком
        if pos > 0:
            pos -= reduce_amt
        else:
            pos += reduce_amt

        closed_now = abs(pos) < 1e-12
        if closed_now and current is not None:
            current["close_time"] = ts
            current["fee_total"] = current["fee_entry"] + current["fee_exit"]
            current["pnl_net"] = current["pnl_realized"] - current["fee_total"]
            rounds.append(current)
            current = None
            direction = 0
            pos = 0.0

        # Перестрёл — старт нового раунда в обратную сторону
        if overshoot > 1e-12:
            # новый раунд стартует прямо этим же трейдом
            direction = sign
            current = {
                "symbol": symbol,
                "side": "LONG" if direction == 1 else "SHORT",
                "qty": overshoot,
                "fee_entry": entry_fee_for_overshoot,
                "fee_exit": 0.0,
                "fee_total": 0.0,
                "pnl_realized": 0.0,
                "pnl_net": 0.0,
                "open_time": ts,
                "close_time": None,
            }
            pos = sign * overshoot

    # Возвращаем последние N закрытых раундов (плоских в конце)
    rounds = rounds[-limit_rounds:]
    rounds.sort(key=lambda r: r.get("close_time") or 0, reverse=True)

    # Нормируем числа до читабельного формата строк
    def fmt(x: float) -> str:
        return f"{x:.10f}".rstrip('0').rstrip('.') if isinstance(x, float) else str(x)

    for r in rounds:
        for k in ("qty", "fee_entry", "fee_exit", "fee_total", "pnl_realized", "pnl_net"):
            r[k] = fmt(r[k])

    return rounds


@app.get("/orders/history.json")
def orders_history(symbol: Optional[str] = None, limit_rounds: int = 50, limit_trades: int = 1000):
    """
    JSON с закрытыми позициями (раунды), для отображения в таблице на orders.html.
    """
    sym = (symbol or SYMBOL_DEFAULT).upper()
    lk = _lock_for(sym)
    with lk:
        try:
            data = _roundtrips_from_trades(sym, limit_rounds=limit_rounds, limit_trades=limit_trades)
        except Exception as e:
            log.warning(f"[HISTORY] failed: {e}", exc_info=True)
            data = []
    return {"symbol": sym, "rounds": data}


@app.get("/orders.html", response_class=FileResponse)
def orders_html(symbol: Optional[str] = None):
    """
    Отдаём статическую страницу дашборда.
    Параметр symbol читается на клиенте из query (?symbol=ETHUSDT).
    """
    return FileResponse("static/orders.html")



@app.get("/orders/open")
def orders_open(symbol: Optional[str] = None):
    """
    Разовый JSON-снимок открытых ордеров и позиции по символу (по умолчанию SYMBOL_DEFAULT).
    """
    sym = (symbol or SYMBOL_DEFAULT).upper()
    # (опционально) блокируем на время чтения, чтобы не пересекаться с записью
    lk = _lock_for(sym)
    with lk:
        data = _orders_snapshot(sym)
    return data

async def _sse_gen(symbol: str, interval_ms: int):
    sym = symbol.upper()
    interval = max(200, int(interval_ms)) / 1000.0  # защита от слишком частых запросов
    while True:
        try:
            lk = _lock_for(sym)
            with lk:
                payload = _orders_snapshot(sym)
            yield f"data: {json.dumps(payload)}\n\n"
        except Exception as e:
            # отправим "пустое" событие, чтобы держать соединение
            yield f"event: ping\ndata: keepalive\n\n"
            log.warning(f"[SSE] error: {e}", exc_info=True)
        await asyncio.sleep(interval)

@app.get("/orders/stream")
async def orders_stream(symbol: Optional[str] = None, interval_ms: int = 1000):
    """
    Server-Sent Events поток с данными по открытым ордерам/позиции.
    Пример: /orders/stream?symbol=ETHUSDT&interval_ms=1000
    """
    sym = (symbol or SYMBOL_DEFAULT).upper()
    return StreamingResponse(_sse_gen(sym, interval_ms), media_type="text/event-stream")


# Раздача статических файлов (папка static/)
app.mount("/static", StaticFiles(directory="static", html=True), name="static")


# Совместимость со старым URL: /orders?symbol=ETHUSDT -> редирект на /orders.html?symbol=ETHUSDT
@app.get("/orders")
def orders_redirect(symbol: Optional[str] = None):
    q = f"?symbol={symbol}" if symbol else ""
    return RedirectResponse(url=f"/orders.html{q}")


@app.get("/settings/tpsl")
def get_tpsl_settings():
    return {
        "tp_enabled": _runtime_opts["tp_enabled"],
        "sl_enabled": _runtime_opts["sl_enabled"],
        "tp_pct": TP_PCT,
        "sl_pct": SL_PCT,
    }

@app.post("/settings/tpsl")
def set_tpsl_settings(payload: TpSlSettingsPayload):
    changed = False
    if payload.tp_enabled is not None:
        _runtime_opts["tp_enabled"] = bool(payload.tp_enabled)
        changed = True
    if payload.sl_enabled is not None:
        _runtime_opts["sl_enabled"] = bool(payload.sl_enabled)
        changed = True

    # Если что-то выключили — очищаем текущие TP/SL по символу (по умолчанию SYMBOL_DEFAULT)
    symbol = (payload.symbol or SYMBOL_DEFAULT).upper()
    if changed and (not _runtime_opts["tp_enabled"] or not _runtime_opts["sl_enabled"]):
        try:
            om = build_manager(symbol, QTY_DEFAULT)
            om.cancel_exit_orders()
        except Exception as e:
            log.warning(f"[SETTINGS] cancel exits failed for {symbol}: {e}", exc_info=True)

    return {
        "status": "ok",
        "tp_enabled": _runtime_opts["tp_enabled"],
        "sl_enabled": _runtime_opts["sl_enabled"],
        "symbol": symbol,
    }



if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
