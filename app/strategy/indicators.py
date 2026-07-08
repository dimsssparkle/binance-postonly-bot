"""Чистые функции-индикаторы — считаем сами, без сторонних библиотек, чтобы
исключить скрытый lookahead/repainting и иметь возможность юнит-тестировать
против рассчитанных вручную значений.

Все функции работают ТОЛЬКО с переданными данными (которые уже point-in-time
из MarketView), поэтому заглянуть в будущее физически нельзя. Возвращают None,
если данных не хватает на полный период — стратегия обязана это проверять,
а не получать «додуманное» значение.
"""
from __future__ import annotations
from typing import Optional, Sequence

from app.backtest.candle import Candle


def sma(values: Sequence[float], period: int) -> Optional[float]:
    """Простая скользящая средняя последних `period` значений."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: Sequence[float], period: int) -> Optional[float]:
    """Экспоненциальная скользящая средняя. Сидируется SMA первых `period`
    значений, затем экспоненциальное сглаживание по остальным."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    """RSI по Уайлдеру (сглаживание средних приростов/убытков).
    Нужно минимум period+1 значений (period изменений)."""
    if period <= 0 or len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    """Average True Range (по Уайлдеру). Нужно минимум period+1 свечей."""
    if period <= 0 or len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def bollinger(closes: Sequence[float], period: int = 20, k: float = 2.0):
    """Полосы Боллинджера -> (lower, mid, upper). None, если данных мало."""
    if period <= 0 or len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = var ** 0.5
    return (mid - k * sd, mid, mid + k * sd)


def rolling_high(candles: Sequence[Candle], n: int) -> Optional[float]:
    """Максимум high за последние `n` свечей."""
    if n <= 0 or len(candles) < n:
        return None
    return max(c.high for c in candles[-n:])


def rolling_low(candles: Sequence[Candle], n: int) -> Optional[float]:
    """Минимум low за последние `n` свечей."""
    if n <= 0 or len(candles) < n:
        return None
    return min(c.low for c in candles[-n:])
