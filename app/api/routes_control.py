from __future__ import annotations
import logging

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import ManualTradePayload, TpSlSettingsPayload
from app.engine.exceptions import EngineBusyError, EngineFailure
from app.engine.models import Intent, Side
from app.engine.state_machine import ExecutionEngine

log = logging.getLogger("api.control")
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


def _get_engine(request: Request) -> ExecutionEngine:
    return request.app.state.engine


@router.get("/healthz")
def health():
    return {"ok": True}


@router.post("/trade/manual")
async def manual_trade(payload: ManualTradePayload, request: Request):
    engine = _get_engine(request)
    side = Side(payload.side)
    qty_str = str(payload.qty) if payload.qty is not None else None
    try:
        intent = await engine.handle_signal(side, qty=qty_str)
    except EngineBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except EngineFailure as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok", "intent": _intent_to_dict(intent)}


@router.post("/trade/close")
async def close_trade(request: Request):
    engine = _get_engine(request)
    try:
        intent = await engine.handle_signal(Side.FLAT)
    except EngineBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except EngineFailure as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok", "intent": _intent_to_dict(intent)}


@router.get("/settings/tpsl")
def get_tpsl_settings(request: Request):
    engine = _get_engine(request)
    return {"tp_pct": engine.tp_pct, "sl_pct": engine.sl_pct}


@router.post("/settings/tpsl")
async def set_tpsl_settings(payload: TpSlSettingsPayload, request: Request):
    engine = _get_engine(request)
    settings = request.app.state.settings
    if payload.tp_pct is not None:
        engine.tp_pct = payload.tp_pct
        await settings.set("tp_pct", str(payload.tp_pct))
    if payload.sl_pct is not None:
        engine.sl_pct = payload.sl_pct
        await settings.set("sl_pct", str(payload.sl_pct))
    return {"status": "ok", "tp_pct": engine.tp_pct, "sl_pct": engine.sl_pct}
