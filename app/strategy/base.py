from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional

from app.engine.models import Signal


class Strategy(ABC):
    """
    Интерфейс источника сигналов. Реальная стратегия (индикаторы, правила,
    ML-модель — что угодно) будет отдельной реализацией этого интерфейса —
    движок не знает и не должен знать, откуда взялся сигнал, поэтому
    ручной /trade/manual и будущая автоматическая стратегия работают
    через один и тот же путь (ExecutionEngine.handle_signal).
    """

    @abstractmethod
    async def evaluate(self) -> Optional[Signal]:
        """Вернуть новый сигнал, если стратегия решила его сгенерировать, иначе None."""
        raise NotImplementedError
