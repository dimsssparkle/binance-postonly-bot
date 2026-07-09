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


def adx(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    """Average Directional Index (по Уайлдеру) — сила ТРЕНДА 0..100, без
    направления (в отличие от ATR/Bollinger, которые мерят амплитуду
    волатильности, а не направленную устойчивость движения — рынок может
    сильно колебаться между двумя уровнями с нулевым чистым прогрессом, и
    ADX это отличит от настоящего тренда). Нужно минимум 2*period+1 свечей:
    period на сидирование сглаживания TR/+DM/-DM, ещё period на сидирование
    сглаживания итогового DX в ADX."""
    if period <= 0 or len(candles) < 2 * period + 1:
        return None
    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, len(candles)):
        h, l = candles[i].high, candles[i].low
        ph, pl, pc = candles[i - 1].high, candles[i - 1].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up_move, down_move = h - ph, pl - l
        plus_dms.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dms.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    def _wilder_smooth(values: list) -> list:
        s = sum(values[:period])
        out = [s]
        for v in values[period:]:
            s = s - (s / period) + v
            out.append(s)
        return out

    tr_s = _wilder_smooth(trs)
    pdm_s = _wilder_smooth(plus_dms)
    mdm_s = _wilder_smooth(minus_dms)

    dxs = []
    for tr_v, pdm_v, mdm_v in zip(tr_s, pdm_s, mdm_s):
        if tr_v == 0:
            dxs.append(0.0)
            continue
        plus_di, minus_di = 100.0 * pdm_v / tr_v, 100.0 * mdm_v / tr_v
        denom = plus_di + minus_di
        dxs.append(100.0 * abs(plus_di - minus_di) / denom if denom else 0.0)

    if len(dxs) < period:
        return None
    a = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        a = (a * (period - 1) + dx) / period
    return a
