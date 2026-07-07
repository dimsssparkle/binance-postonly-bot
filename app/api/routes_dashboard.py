from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.config import LEVERAGE_DEFAULT
from app.engine.models import Intent

log = logging.getLogger("api.dashboard")
router = APIRouter()


def _intent_to_dict(intent: Intent) -> dict:
    return {
        "id": intent.id,
        "symbol": intent.symbol,
        "desired_side": intent.desired_side.value,
        "qty": intent.qty,
        "state": intent.state.value,
        "attempt_no": intent.attempt_no,
        "entry_price": intent.entry_price,
        "failure_reason": intent.failure_reason,
        "created_at_ms": intent.created_at_ms,
        "updated_at_ms": intent.updated_at_ms,
    }


def _position_snapshot(request: Request, symbol: str) -> dict[str, Any]:
    rest = request.app.state.rest
    sym = symbol.upper()

    position: dict[str, Any] = {
        "symbol": sym, "positionAmt": "0", "entryPrice": "0",
        "markPrice": "0", "unRealizedProfit": "0", "leverage": str(LEVERAGE_DEFAULT),
    }
    try:
        for p in rest.get_position_risk(sym) or []:
            if str(p.get("symbol", "")).upper() == sym:
                # Binance omits flat symbols from position_risk entirely, so
                # this branch (and its real leverage figure) is only hit while
                # a position is actually open — the dict default above covers
                # the flat case with the configured leverage instead of "0".
                position = {
                    "symbol": sym,
                    "positionAmt": p.get("positionAmt", "0"),
                    "entryPrice": p.get("entryPrice", "0"),
                    "markPrice": p.get("markPrice", "0"),
                    "unRealizedProfit": p.get("unRealizedProfit", "0"),
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
def orders_open(request: Request, symbol: Optional[str] = None):
    engine = request.app.state.engine
    sym = (symbol or engine.symbol).upper()
    return _position_snapshot(request, sym)


@router.get("/intents")
async def list_intents(request: Request, limit: int = 30):
    engine = request.app.state.engine
    rows = await engine.intents.list_recent(limit)
    return {"intents": [_intent_to_dict(i) for i in rows]}


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
            payload = {
                "position": _position_snapshot(request, symbol),
                "intents": [_intent_to_dict(i) for i in await engine.intents.list_recent(10)],
                "events": await engine.events.tail(15),
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
