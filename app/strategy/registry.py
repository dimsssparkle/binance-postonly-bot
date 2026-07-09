"""Явный opt-in реестр стратегий, доступных для настройки с дашборда.

Специально НЕ автообнаружение через Strategy.__subclasses__() — это держит
sanity.py (AlwaysLong/Random — калибровка бэктестера, не боевые) и
NoopStrategy (боевой дефолт до готовности реальной стратегии) вне
дашборда навсегда, без риска случайно "протечь" туда в будущем.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from app.strategy.base import Strategy
from app.strategy.mean_reversion import MeanReversionStrategy
from app.strategy.momentum import MomentumStrategy
from app.strategy.params import ParamSpec, ParamType, validate_params
from app.strategy.regime_router import RegimeRouterStrategy

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
    "regime_router": StrategyMeta(
        key="regime_router", label="Regime Router (ADX: тренд/флэт)", cls=RegimeRouterStrategy,
        params=[
            ParamSpec("adx_period", ParamType.INT, 14, "Период ADX", min=2, max=100),
            ParamSpec("adx_threshold", ParamType.FLOAT, 25.0, "Порог ADX (тренд, если выше)", min=1, max=100),
            ParamSpec("tf", ParamType.ENUM, "15m", "Таймфрейм (классификация и кандидаты)", choices=TF_CHOICES),
            ParamSpec("trending_config_id", ParamType.STRATEGY_REF, 0, "Кандидат: тренд"),
            ParamSpec("ranging_config_id", ParamType.STRATEGY_REF, 0, "Кандидат: флэт"),
        ],
    ),
}

# STRATEGY_REF-параметры не совпадают по имени с kwarg конструктора
# (trending_config_id — это ссылка на другую strategy_configs-запись, а
# конструктору RegimeRouterStrategy нужен уже готовый объект Strategy под
# именем trending_strategy) — явная карта переименования на разрешении.
_STRATEGY_REF_KWARG_MAP: dict[str, dict[str, str]] = {
    "regime_router": {"trending_config_id": "trending_strategy", "ranging_config_id": "ranging_strategy"},
}


def build_strategy(strategy_key: str, params: dict,
                    sub_strategies: Optional[dict[str, Strategy]] = None) -> Strategy:
    """sub_strategies: для STRATEGY_REF-параметров — уже РАЗРЕШЁННЫЕ (не id,
    а готовые построенные) Strategy-инстансы, keyed по имени ParamSpec (не по
    имени kwarg — переименование см. _STRATEGY_REF_KWARG_MAP). Разрешение
    id->Strategy требует доступа к репозиторию, которого у этой чистой
    функции нет — это ответственность вызывающего (routes_strategies.py)."""
    meta = STRATEGY_REGISTRY.get(strategy_key)
    if meta is None:
        raise ValueError(f"неизвестная стратегия: {strategy_key!r}")
    clean = validate_params(meta.params, params)

    ref_names = [p.name for p in meta.params if p.type == ParamType.STRATEGY_REF]
    if ref_names:
        if sub_strategies is None:
            raise ValueError(f"{strategy_key}: нужны разрешённые sub_strategies для {ref_names}")
        kwarg_map = _STRATEGY_REF_KWARG_MAP.get(strategy_key, {})
        for name in ref_names:
            clean.pop(name)
            if name not in sub_strategies:
                raise ValueError(f"{strategy_key}: отсутствует sub_strategies[{name!r}]")
            clean[kwarg_map.get(name, name)] = sub_strategies[name]

    return meta.cls(**clean)
