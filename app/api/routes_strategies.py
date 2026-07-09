"""CRUD для конфигураций стратегий + запуск бэктеста с дашборда.

Фаза A: чисто конфигурация + research-инструмент. Ничего здесь не
подключено к боевой торговле — StrategyRunner/ExecutionEngine не тронуты.
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    BacktestRunPayload, EnabledPayload, StrategyConfigCreatePayload, StrategyConfigUpdatePayload,
)
from app.backtest.data import get_history
from app.backtest.engine import BacktestConfig, BacktestEngine
from app.backtest.report import build_report
from app.strategy.params import validate_params
from app.strategy.registry import STRATEGY_REGISTRY, build_strategy

log = logging.getLogger("api.strategies")
router = APIRouter()


def _param_spec_to_dict(spec) -> dict[str, Any]:
    d = asdict(spec)
    d["type"] = spec.type.value
    return d


@router.get("/strategies/types")
def list_strategy_types():
    return {
        key: {"label": meta.label, "params": [_param_spec_to_dict(p) for p in meta.params]}
        for key, meta in STRATEGY_REGISTRY.items()
    }


@router.get("/strategies/configs")
async def list_strategy_configs(request: Request):
    repo = request.app.state.strategy_configs
    return {"configs": await repo.list_all()}


@router.post("/strategies/configs")
async def create_strategy_config(payload: StrategyConfigCreatePayload, request: Request):
    meta = STRATEGY_REGISTRY.get(payload.strategy_key)
    if meta is None:
        raise HTTPException(status_code=400, detail=f"неизвестная стратегия: {payload.strategy_key}")
    try:
        clean = validate_params(meta.params, payload.params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    repo = request.app.state.strategy_configs
    config = await repo.create(payload.strategy_key, payload.name, clean)
    return {"config": config}


@router.put("/strategies/configs/{config_id}")
async def update_strategy_config(config_id: int, payload: StrategyConfigUpdatePayload, request: Request):
    repo = request.app.state.strategy_configs
    existing = await repo.get(config_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="конфигурация не найдена")
    meta = STRATEGY_REGISTRY[existing["strategy_key"]]
    try:
        clean = validate_params(meta.params, payload.params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await repo.update_params(config_id, payload.name, clean)
    return {"config": await repo.get(config_id)}


@router.post("/strategies/configs/{config_id}/enabled")
async def set_strategy_config_enabled(config_id: int, payload: EnabledPayload, request: Request):
    repo = request.app.state.strategy_configs
    existing = await repo.get(config_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="конфигурация не найдена")
    await repo.set_enabled(config_id, payload.enabled)
    return {"config": await repo.get(config_id)}


@router.delete("/strategies/configs/{config_id}")
async def delete_strategy_config(config_id: int, request: Request):
    repo = request.app.state.strategy_configs
    await repo.delete(config_id)
    return {"status": "ok"}


def _run_backtest_sync(rest, strategy_key: str, params: dict, payload: BacktestRunPayload) -> dict:
    """Блокирующая часть (чтение кэша истории с диска + прогон движка) —
    выполняется в threadpool через asyncio.to_thread, чтобы не подвешивать
    event loop (и параллельные SSE-стримы дашборда) на время бэктеста."""
    meta = STRATEGY_REGISTRY.get(strategy_key)
    if meta is None:
        raise ValueError(f"неизвестная стратегия: {strategy_key}")
    strategy = build_strategy(strategy_key, params)

    entry_tf = payload.entry_tf or params.get("tf") or "15m"
    exit_tf = payload.exit_tf

    entry_candles = get_history(rest, payload.symbol, entry_tf, payload.days)
    exit_candles = (entry_candles if exit_tf == entry_tf
                    else get_history(rest, payload.symbol, exit_tf, payload.days))
    if not entry_candles or not exit_candles:
        raise ValueError(
            f"нет закэшированной истории для {payload.symbol} {entry_tf}/{exit_tf} "
            f"(backtest_data/) — Фаза A не тянет её из Binance по клику")

    cfg = BacktestConfig(
        entry_tf=entry_tf, exit_tf=exit_tf, exit_mode=payload.exit_mode,
        tp_pct=payload.tp_pct, sl_pct=payload.sl_pct,
    )
    result = BacktestEngine(cfg).run(strategy, {entry_tf: entry_candles, exit_tf: exit_candles})
    report = build_report(result)

    trades = [
        {
            "side": t.side.value, "entry_time_ms": t.entry_time_ms, "entry_price": t.entry_price,
            "exit_time_ms": t.exit_time_ms, "exit_price": t.exit_price, "net_pnl": t.net_pnl,
            "net_return": t.net_return, "exit_reason": t.exit_reason, "bars_held": t.bars_held,
        }
        for t in result.trades[-200:]
    ]
    return {"report": vars(report), "trades": trades}


@router.post("/strategies/backtest")
async def run_strategy_backtest(payload: BacktestRunPayload, request: Request):
    if payload.config_id is not None:
        repo = request.app.state.strategy_configs
        config = await repo.get(payload.config_id)
        if config is None:
            raise HTTPException(status_code=404, detail="конфигурация не найдена")
        strategy_key, params = config["strategy_key"], config["params"]
    else:
        strategy_key, params = payload.strategy_key, (payload.params or {})
        if strategy_key not in STRATEGY_REGISTRY:
            raise HTTPException(status_code=400, detail=f"неизвестная стратегия: {strategy_key}")

    try:
        clean_params = validate_params(STRATEGY_REGISTRY[strategy_key].params, params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    rest = request.app.state.rest
    try:
        return await asyncio.to_thread(_run_backtest_sync, rest, strategy_key, clean_params, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
