"""Явный opt-in реестр стратегий, доступных для настройки с дашборда.

Специально НЕ автообнаружение через Strategy.__subclasses__() — это держит
sanity.py (AlwaysLong/Random — калибровка бэктестера, не боевые) и
NoopStrategy (боевой дефолт до готовности реальной стратегии) вне
дашборда навсегда, без риска случайно "протечь" туда в будущем.
"""
from __future__ import annotations
from dataclasses import dataclass

from app.strategy.base import Strategy
from app.strategy.mean_reversion import MeanReversionStrategy
from app.strategy.momentum import MomentumStrategy
from app.strategy.params import ParamSpec, ParamType, validate_params

# Тот же список таймфреймов, что backtest/data.py::_TF_MINUTES поддерживает.
TF_CHOICES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]


@dataclass(frozen=True)
class StrategyMeta:
    key: str
    label: str
    cls: type[Strategy]
    params: list[ParamSpec]


STRATEGY_REGISTRY: dict[str, StrategyMeta] = {
    "momentum": StrategyMeta(
        key="momentum", label="Momentum (пробой)", cls=MomentumStrategy,
        params=[
            ParamSpec("lookback", ParamType.INT, 20, "Окно пробоя (баров)", min=5, max=200),
            ParamSpec("flow_long", ParamType.FLOAT, 0.55, "Мин. доля покупок (long)", min=0.5, max=1.0),
            ParamSpec("flow_short", ParamType.FLOAT, 0.45, "Макс. доля покупок (short)", min=0.0, max=0.5),
            ParamSpec("tf", ParamType.ENUM, "15m", "Таймфрейм", choices=TF_CHOICES),
        ],
    ),
    "mean_reversion": StrategyMeta(
        key="mean_reversion", label="Mean Reversion (RSI)", cls=MeanReversionStrategy,
        params=[
            ParamSpec("period", ParamType.INT, 14, "Период RSI", min=2, max=100),
            ParamSpec("oversold", ParamType.FLOAT, 30.0, "Порог перепроданности", min=1, max=49),
            ParamSpec("overbought", ParamType.FLOAT, 70.0, "Порог перекупленности", min=51, max=99),
            ParamSpec("flow_filter", ParamType.BOOL, True, "Фильтр по потоку"),
            ParamSpec("tf", ParamType.ENUM, "15m", "Таймфрейм", choices=TF_CHOICES),
        ],
    ),
}


def build_strategy(strategy_key: str, params: dict) -> Strategy:
    meta = STRATEGY_REGISTRY.get(strategy_key)
    if meta is None:
        raise ValueError(f"неизвестная стратегия: {strategy_key!r}")
    clean = validate_params(meta.params, params)
    return meta.cls(**clean)
