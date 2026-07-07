from __future__ import annotations
import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.config import LEVERAGE_DEFAULT
from app.engine.analytics import daily_net_pnl, round_trip_commission
from app.engine.models import Intent

log = logging.getLogger("api.dashboard")
router = APIRouter()


async def _intent_to_dict(request: Request, intent: Intent) -> dict:
    engine = request.app.state.engine
    closing = await engine.orders.get_closing_fill(intent.id)
    if closing is not None:
        total_commission = await round_trip_commission(engine.intents, engine.orders, intent, closing)
        net_realized_pnl = Decimal(closing.realized_pnl or "0") - total_commission
    else:
        total_commission = await engine.orders.sum_all_commission(intent.id)
        net_realized_pnl = None
    return {
        "id": intent.id,
        "symbol": intent.symbol,
        "desired_side": intent.desired_side.value,
        "qty": intent.qty,
        "state": intent.state.value,
        "attempt_no": intent.attempt_no,
        "entry_price": intent.entry_price,
        "close_price": closing.filled_price if closing else None,
        "net_realized_pnl": str(net_realized_pnl) if net_realized_pnl is not None else None,
        "total_commission": str(total_commission),
        "failure_reason": intent.failure_reason,
        "created_at_ms": intent.created_at_ms,
        "updated_at_ms": intent.updated_at_ms,
    }


async def _net_unrealized_pnl(request: Request, sym: str, gross_pnl: float,
                               mark_price: float, position_amt: float) -> Optional[str]:
    """Gross uPnL минус фактическая комиссия входа (уже уплачена) минус
    оценочная комиссия закрытия по текущей mark price (taker, TP/SL —
    Algo Order). None, если нет активного intent-а — тогда делить не на что."""
    engine = request.app.state.engine
    intent = await engine.intents.get_active(sym)
    if intent is None:
        return None
    try:
        entry_fee = await engine.orders.sum_entry_commission(intent.id)
        taker_rate = engine.commission_rates.get(sym)["taker"]
    except Exception as e:
        log.warning(f"[DASHBOARD] net pnl calc failed: {e}")
        return None
    exit_fee_est = abs(position_amt) * mark_price * taker_rate
    net = gross_pnl - float(entry_fee) - exit_fee_est
    return f"{net:.8f}"


async def _position_snapshot(request: Request, symbol: str) -> dict[str, Any]:
    rest = request.app.state.rest
    sym = symbol.upper()

    position: dict[str, Any] = {
        "symbol": sym, "positionAmt": "0", "entryPrice": "0",
        "markPrice": "0", "unRealizedProfit": "0", "netUnrealizedProfit": None,
        "leverage": str(LEVERAGE_DEFAULT),
    }
    try:
        for p in rest.get_position_risk(sym) or []:
            if str(p.get("symbol", "")).upper() == sym:
                # Binance omits flat symbols from position_risk entirely, so
                # this branch (and its real leverage figure) is only hit while
                # a position is actually open — the dict default above covers
                # the flat case with the configured leverage instead of "0".
                gross_pnl = float(p.get("unRealizedProfit", "0") or 0)
                mark_price = float(p.get("markPrice", "0") or 0)
                position_amt = float(p.get("positionAmt", "0") or 0)
                position = {
                    "symbol": sym,
                    "positionAmt": p.get("positionAmt", "0"),
                    "entryPrice": p.get("entryPrice", "0"),
                    "markPrice": p.get("markPrice", "0"),
                    "unRealizedProfit": p.get("unRealizedProfit", "0"),
                    "netUnrealizedProfit": await _net_unrealized_pnl(
                        request, sym, gross_pnl, mark_price, position_amt),
                    "leverage": p.get("leverage") or str(LEVERAGE_DEFAULT),
                }
                break
    except Exception as e:
        log.warning(f"[DASHBOARD] position_risk failed: {e}")

    book: dict[str, Any] = {}
    try:
        bt = rest.book_ticker(sym) or {}
        book = {"bidPrice": bt.get("bidPrice"), "askPrice": bt.get("askPrice")}
    except Exception as e:
        log.warning(f"[DASHBOARD] book_ticker failed: {e}")

    return {"symbol": sym, "position": position, "book": book, "ts": int(time.time() * 1000)}


@router.get("/dashboard.html", response_class=FileResponse)
def dashboard_html():
    return FileResponse("static/dashboard.html")


@router.get("/orders/open")
async def orders_open(request: Request, symbol: Optional[str] = None):
    engine = request.app.state.engine
    sym = (symbol or engine.symbol).upper()
    return await _position_snapshot(request, sym)


@router.get("/intents")
async def list_intents(request: Request, limit: int = 30):
    engine = request.app.state.engine
    rows = await engine.intents.list_recent(limit)
    return {"intents": [await _intent_to_dict(request, i) for i in rows]}


@router.get("/pnl/daily")
async def pnl_daily(request: Request, symbol: Optional[str] = None):
    engine = request.app.state.engine
    sym = (symbol or engine.symbol).upper()
    total = await daily_net_pnl(engine.intents, engine.orders, sym)
    return {"symbol": sym, "net_pnl_today": str(total)}


@router.get("/events")
async def list_events(request: Request, limit: int = 50):
    engine = request.app.state.engine
    rows = await engine.events.tail(limit)
    return {"events": rows}


async def _dashboard_sse_gen(request: Request, symbol: str, interval_ms: int):
    engine = request.app.state.engine
    interval = max(500, int(interval_ms)) / 1000.0
    while True:
        if await request.is_disconnected():
            break
        try:
            intents_rows = await engine.intents.list_recent(10)
            payload = {
                "position": await _position_snapshot(request, symbol),
                "intents": [await _intent_to_dict(request, i) for i in intents_rows],
                "events": await engine.events.tail(15),
                "netPnlToday": str(await daily_net_pnl(engine.intents, engine.orders, symbol)),
            }
            yield f"data: {json.dumps(payload)}\n\n"
        except Exception as e:
            log.warning(f"[DASHBOARD SSE] error: {e}", exc_info=True)
            yield "event: ping\ndata: keepalive\n\n"
        await asyncio.sleep(interval)


@router.get("/orders/stream")
async def orders_stream(request: Request, symbol: Optional[str] = None, interval_ms: int = 2000):
    engine = request.app.state.engine
    sym = (symbol or engine.symbol).upper()
    return StreamingResponse(_dashboard_sse_gen(request, sym, interval_ms), media_type="text/event-stream")
