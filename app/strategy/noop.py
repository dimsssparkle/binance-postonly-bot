from __future__ import annotations
from typing import Optional

from app.engine.models import Signal
from app.strategy.base import Strategy


class NoopStrategy(Strategy):
    """Заглушка — никогда сама не генерирует сигналы. Движок до готовности
    реальной стратегии управляется только вручную (/trade/manual, /trade/close),
    но уже через тот же StrategyRunner-путь, что будет использовать боевая
    стратегия."""

    async def evaluate(self) -> Optional[Signal]:
        return None
