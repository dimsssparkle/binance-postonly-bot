"""MarketView — point-in-time окно рыночных данных, которое видит стратегия.

Ключевая гарантия против repainting: MarketView отдаёт ТОЛЬКО свечи, закрытые
не позже `now_ms` (момент принятия решения). Заглянуть в будущую свечу
структурно невозможно.

Реализация хранит ССЫЛКУ на полный список свечей + число видимых (граница
находится бинарным поиском по close_time_ms, O(log n)) — без копирования и без
O(n) фильтра. Это важно: бэктест конструирует MarketView сотни тысяч раз.
Предполагается, что свечи отсортированы по возрастанию времени.

Один и тот же класс наполняется:
  - в live: закрытыми klines из REST/WS (текущая формирующаяся свеча исключена),
  - в бэктесте: воспроизведением истории (now_ms двигается вперёд).
Стратегия не видит разницы -> бэктест честен по построению.
"""
from __future__ import annotations
from bisect import bisect_right
from typing import Optional, Sequence

from app.backtest.candle import Candle


class MarketView:
    def __init__(self, candles_by_tf: dict[str, Sequence[Candle]], now_ms: int) -> None:
        self.now_ms = now_ms
        # {tf: (full_candles_ref, visible_count)}; count — сколько свечей с
        # close_time_ms <= now_ms (граница будущего).
        self._w: dict[str, tuple[Sequence[Candle], int]] = {}
        for tf, cs in candles_by_tf.items():
            cnt = bisect_right(cs, now_ms, key=lambda c: c.close_time_ms)
            self._w[tf] = (cs, cnt)

    def timeframes(self) -> list[str]:
        return list(self._w.keys())

    def candles(self, tf: str, n: Optional[int] = None) -> list[Candle]:
        """Последние `n` закрытых свечей таймфрейма `tf` (oldest->newest).
        n=None -> все видимые."""
        cs, cnt = self._w.get(tf, ((), 0))
        start = 0 if n is None else max(0, cnt - n)
        return list(cs[start:cnt])

    def closes(self, tf: str, n: Optional[int] = None) -> list[float]:
        return [c.close for c in self.candles(tf, n)]

    def last(self, tf: str) -> Optional[Candle]:
        cs, cnt = self._w.get(tf, ((), 0))
        return cs[cnt - 1] if cnt > 0 else None

    def last_price(self, tf: str = "1m") -> Optional[float]:
        """Цена close самой свежей закрытой свечи (по умолчанию — младший TF).
        Фолбэк на любой доступный TF, если запрошенного нет/пуст."""
        c = self.last(tf)
        if c is not None:
            return c.close
        for other in self._w:
            c = self.last(other)
            if c is not None:
                return c.close
        return None
