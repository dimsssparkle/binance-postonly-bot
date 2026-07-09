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
from app.strategy.base import Strategy
from app.strategy.params import ParamType, validate_params
from app.strategy.registry import STRATEGY_REGISTRY, StrategyMeta, build_strategy

log = logging.getLogger("api.strategies")
router = APIRouter()


def _param_spec_to_dict(spec) -> dict[str, Any]:
    d = asdict(spec)
    d["type"] = spec.type.value
    return d


async def _resolve_sub_strategies(repo, meta: StrategyMeta, params: dict) -> dict[str, Strategy]:
    """Для STRATEGY_REF-параметров meta: разрешает id -> реальный построенный
    Strategy (существование конфигурации, отсутствие вложенных roter'ов,
    совпадение tf с родителем — иначе кандидат с другим tf будет молча
    получать пустое окно свечей от market.candles(tf,...) и навсегда
    отвечать HOLD/warmup, см. Phase B план)."""
    ref_specs = [p for p in meta.params if p.type == ParamType.STRATEGY_REF]
    if not ref_specs:
        return {}
    router_tf = params.get("tf")
    resolved: dict[str, Strategy] = {}
    for spec in ref_specs:
        config_id = params.get(spec.name) or 0
        if not config_id:
            raise ValueError(f"{spec.label}: не выбран кандидат")
        sub_config = await repo.get(config_id)
        if sub_config is None:
            raise ValueError(f"{spec.label}: конфигурация #{config_id} не найдена")
        if sub_config["strategy_key"] == "regime_router":
            raise ValueError(f"{spec.label}: вложенные regime_router не поддерживаются")
        sub_tf = sub_config["params"].get("tf")
        if sub_tf is not None and router_tf is not None and sub_tf != router_tf:
            raise ValueError(
                f"{spec.label}: таймфрейм кандидата ({sub_tf}) не совпадает с "
                f"таймфреймом роутера ({router_tf})")
        resolved[spec.name] = build_strategy(sub_config["strategy_key"], sub_config["params"])
    return resolved


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
    try:
        await _resolve_sub_strategies(repo, meta, clean)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    try:
        await _resolve_sub_strategies(repo, meta, clean)
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


def _run_backtest_sync(rest, strategy_key: str, params: dict, payload: BacktestRunPayload,
                        sub_strategies: Optional[dict[str, Strategy]] = None) -> dict:
    """Блокирующая часть (чтение кэша истории с диска + прогон движка) —
    выполняется в threadpool через asyncio.to_thread, чтобы не подвешивать
    event loop (и параллельные SSE-стримы дашборда) на время бэктеста.
    sub_strategies уже разрешены (id->Strategy) вызывающим ДО этого хэндова
    в поток — здесь никакого доступа к репозиторию нет и быть не должно."""
    meta = STRATEGY_REGISTRY.get(strategy_key)
    if meta is None:
        raise ValueError(f"неизвестная стратегия: {strategy_key}")
    strategy = build_strategy(strategy_key, params, sub_strategies)

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
            "entry_reason": t.entry_reason,
        }
        for t in result.trades[-200:]
    ]
    return {"report": vars(report), "trades": trades}


@router.post("/strategies/backtest")
async def run_strategy_backtest(payload: BacktestRunPayload, request: Request):
    repo = request.app.state.strategy_configs
    if payload.config_id is not None:
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

    try:
        sub_strategies = await _resolve_sub_strategies(repo, STRATEGY_REGISTRY[strategy_key], clean_params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    rest = request.app.state.rest
    try:
        return await asyncio.to_thread(
            _run_backtest_sync, rest, strategy_key, clean_params, payload, sub_strategies)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
