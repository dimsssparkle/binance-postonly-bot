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

_TF_HELP = ("Таймфрейм свечей, на которых стратегия принимает решения о входе "
            "(на исполнение уже открытой сделки на бирже не влияет). Меньше "
            "(1m/5m) — сигналов больше, но больше и шума/ложных сигналов; "
            "больше (1h/4h) — сигналов реже, но они обычно надёжнее. 15m — "
            "разумная золотая середина, с неё стоит начинать.")


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
            ParamSpec("lookback", ParamType.INT, 20, "Окно пробоя (баров)", min=5, max=200, help=(
                "Сколько последних свечей стратегия берёт, чтобы найти недавний "
                "максимум/минимум цены. Вход происходит, когда текущая цена "
                "пробивает этот максимум (LONG) или минимум (SHORT) — то есть "
                "цена ушла дальше, чем за все последние N баров. Меньше значение "
                "— сигналы чаще, но больше ложных пробоев; больше — сигналы реже, "
                "но пробой обычно значимее.")),
            ParamSpec("flow_long", ParamType.FLOAT, 0.55, "Мин. доля покупок (long)", min=0.5, max=1.0, help=(
                "Минимальная доля агрессивных покупок в объёме свечи пробоя, "
                "чтобы разрешить вход в LONG (0.5 = покупателей и продавцов "
                "поровну, 1.0 = все сделки на покупку). Фильтр нужен, чтобы не "
                "входить в пробой, который на самом деле толкают вниз продавцы, "
                "а не покупатели. Выше значение — строже фильтр, сигналов "
                "меньше, но они достовернее.")),
            ParamSpec("flow_short", ParamType.FLOAT, 0.45, "Макс. доля покупок (short)", min=0.0, max=0.5, help=(
                "Зеркально flow_long, но для SHORT: максимальная доля покупок "
                "в объёме свечи, ниже которой считаем, что пробой вниз реально "
                "подтверждён продавцами. Меньше значение — строже фильтр, "
                "сигналов меньше.")),
            ParamSpec("tf", ParamType.ENUM, "15m", "Таймфрейм", choices=TF_CHOICES, help=_TF_HELP),
        ],
    ),
    "mean_reversion": StrategyMeta(
        key="mean_reversion", label="Mean Reversion (RSI)", cls=MeanReversionStrategy,
        params=[
            ParamSpec("period", ParamType.INT, 14, "Период RSI", min=2, max=100, help=(
                "Период индикатора RSI (Relative Strength Index) — измеряет, "
                "насколько сильно и как долго цена двигалась в одну сторону, по "
                "шкале 0-100. Стандартное значение — 14 баров. Меньше — RSI "
                "чувствительнее и реагирует быстрее (сигналов больше, но и шума "
                "больше); больше — более сглаженный, сигналы реже, но надёжнее.")),
            ParamSpec("oversold", ParamType.FLOAT, 30.0, "Порог перепроданности", min=1, max=49, help=(
                "Порог RSI, ниже которого рынок считается 'перепроданным' — "
                "стратегия ждёт отскок вверх и открывает LONG. Чем меньше "
                "значение (например, 20 вместо 30), тем реже срабатывает, но "
                "сигнал сильнее — цена должна упасть сильнее, прежде чем сработает.")),
            ParamSpec("overbought", ParamType.FLOAT, 70.0, "Порог перекупленности", min=51, max=99, help=(
                "Зеркально порогу перепроданности: порог RSI, выше которого "
                "рынок считается 'перекупленным' — стратегия открывает SHORT, "
                "ожидая отката вниз. Чем больше значение (например, 80 вместо "
                "70), тем реже сигнал, но экстремум сильнее.")),
            ParamSpec("flow_filter", ParamType.BOOL, True, "Фильтр по потоку", help=(
                "Дополнительная проверка потока ордеров перед входом: не даёт "
                "открыть LONG на перепроданности, если покупатели уже "
                "полностью выбиты агрессивными продажами (и наоборот для "
                "SHORT) — снижает риск 'поймать нож', войдя против движения, "
                "которое ещё продолжается. Рекомендуется оставить включённым.")),
            ParamSpec("tf", ParamType.ENUM, "15m", "Таймфрейм", choices=TF_CHOICES, help=_TF_HELP),
        ],
    ),
    "regime_router": StrategyMeta(
        key="regime_router", label="Regime Router (ADX: тренд/флэт)", cls=RegimeRouterStrategy,
        params=[
            ParamSpec("adx_period", ParamType.INT, 14, "Период ADX", min=2, max=100, help=(
                "Период индикатора ADX (Average Directional Index) — измеряет "
                "СИЛУ тренда (насколько уверенно цена движется в одном "
                "направлении), от 0 до 100, независимо от того, вверх или вниз. "
                "Стандартное значение — 14 баров, как и у RSI. Меньше — "
                "чувствительнее к недавним изменениям; больше — более "
                "сглаженный, медленнее реагирует на смену режима.")),
            ParamSpec("adx_threshold", ParamType.FLOAT, 25.0, "Порог ADX (тренд, если выше)", min=1, max=100, help=(
                "Порог ADX, выше которого рынок считается 'трендовым' (роутер "
                "включает momentum-кандидата), ниже — 'флэтовым' (включает "
                "кандидата для флэта). 25 — общепринятое стандартное значение. "
                "Понизишь порог — рынок чаще будет считаться трендовым; "
                "повысишь — реже, только при очень уверенном тренде.")),
            ParamSpec("tf", ParamType.ENUM, "15m", "Таймфрейм (классификация и кандидаты)",
                      choices=TF_CHOICES, help=(
                "Таймфрейм, на котором считается ADX и работают оба кандидата. "
                "Должен совпадать с их собственным таймфреймом — роутер это "
                "проверяет и не даст сохранить несовпадение (иначе кандидат с "
                "другим tf молча не получал бы данные и всегда бы отвечал "
                "HOLD).")),
            ParamSpec("trending_config_id", ParamType.STRATEGY_REF, 0, "Кандидат: тренд", help=(
                "Какую сохранённую конфигурацию стратегии включать, когда "
                "рынок определён как трендовый (ADX выше порога). Обычно "
                "имеет смысл выбрать momentum-конфигурацию — она рассчитана "
                "именно на пробои и продолжение движения.")),
            ParamSpec("ranging_config_id", ParamType.STRATEGY_REF, 0, "Кандидат: флэт", help=(
                "Какую конфигурацию включать, когда рынок определён как "
                "флэтовый (ADX ниже порога). Обычно имеет смысл выбрать "
                "mean_reversion — она рассчитана на откаты от экстремумов "
                "внутри бокового диапазона.")),
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
