from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from app.engine.models import Side
from app.strategy.market_view import MarketView


class Action(str, Enum):
    HOLD = "hold"
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"


@dataclass
class Decision:
    action: Action
    reason: str = ""  # человекочитаемая причина — пишется в лог/events для разбора постфактум


@dataclass
class PositionView:
    """Что стратегия знает о текущей позиции в момент решения.
    side=FLAT означает «в рынке нет» — тогда осмысленны только ENTER_*."""
    side: Side
    entry_price: float = 0.0
    qty: float = 0.0
    bars_held: int = 0  # сколько свечей (решений) прошло с момента входа — для time-based выходов


class Strategy(ABC):
    """
    Источник сигналов. Получает point-in-time MarketView и текущую позицию,
    возвращает решение. Тот же объект и тот же вызов в live и в бэктесте —
    это и делает бэктест честным (никакого расхождения логики).

    Контракт:
      - FLAT   -> ENTER_LONG / ENTER_SHORT открывают позицию; иначе HOLD.
      - в позиции -> EXIT закрывает (динамический выход по младшему TF);
        иначе HOLD. Жёсткий fee-aware SL при этом всегда живёт на бирже
        отдельно (crash-proof), стратегия про него не думает.

    decide() ДОЛЖЕН быть чистым от side-effect и не тянуть данные сам —
    только из market. Это инвариант против lookahead.
    """

    @abstractmethod
    def decide(self, market: MarketView, position: PositionView) -> Decision:
        raise NotImplementedError
