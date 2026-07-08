"""MarketView — point-in-time окно рыночных данных, которое видит стратегия.

Ключевая гарантия против repainting: MarketView отдаёт ТОЛЬКО свечи, закрытые
не позже `now_ms` (момент принятия решения). Заглянуть в будущую свечу
структурно невозможно — этого нет в данных, которые получает стратегия.

Один и тот же класс наполняется:
  - в live: закрытыми klines из REST/WS (текущая формирующаяся свеча исключена),
  - в бэктесте: воспроизведением истории до текущего индекса.
Стратегия не видит разницы -> бэктест честен по построению.
"""
from __future__ import annotations
from typing import Optional, Sequence

from app.backtest.candle import Candle


class MarketView:
    def __init__(self, candles_by_tf: dict[str, Sequence[Candle]], now_ms: int) -> None:
        """
        candles_by_tf: {"15m": [...], "1m": [...]} — свечи в порядке oldest->newest.
        now_ms: момент принятия решения. Любая свеча с close_time_ms > now_ms
                отбрасывается (это будущее). Хранимые окна уже отфильтрованы.
        """
        self.now_ms = now_ms
        self._by_tf: dict[str, list[Candle]] = {}
        for tf, cs in candles_by_tf.items():
            # Жёсткая отсечка будущего — гарантия, а не договорённость.
            visible = [c for c in cs if c.close_time_ms <= now_ms]
            self._by_tf[tf] = visible

    def timeframes(self) -> list[str]:
        return list(self._by_tf.keys())

    def candles(self, tf: str, n: Optional[int] = None) -> list[Candle]:
        """Последние `n` закрытых свечей таймфрейма `tf` (oldest->newest).
        n=None -> все доступные. Пустой список, если tf не загружен."""
        cs = self._by_tf.get(tf, [])
        if n is None:
            return list(cs)
        return list(cs[-n:])

    def closes(self, tf: str, n: Optional[int] = None) -> list[float]:
        return [c.close for c in self.candles(tf, n)]

    def last(self, tf: str) -> Optional[Candle]:
        cs = self._by_tf.get(tf, [])
        return cs[-1] if cs else None

    def last_price(self, tf: str = "1m") -> Optional[float]:
        """Цена close самой свежей закрытой свечи (по умолчанию — младший TF).
        Фолбэк на любой доступный TF, если запрошенного нет."""
        c = self.last(tf)
        if c is not None:
            return c.close
        for other in self._by_tf:
            c = self.last(other)
            if c is not None:
                return c.close
        return None
