"""Индикаторы против рассчитанных вручную значений — гарантия, что мы считаем
именно то, что думаем, и никакого lookahead."""
from app.backtest.candle import Candle
from app.strategy import indicators as ind


def _c(close, high=None, low=None):
    high = high if high is not None else close
    low = low if low is not None else close
    return Candle(0, close, high, low, close, 100.0, 0, 10, 60.0)


def test_sma_basic():
    assert ind.sma([1, 2, 3, 4, 5], 5) == 3.0
    assert ind.sma([1, 2, 3, 4, 5], 2) == 4.5
    assert ind.sma([1, 2], 5) is None  # мало данных


def test_ema_known():
    # EMA(3) of [1,2,3,4,5]: seed=SMA(1,2,3)=2; k=0.5
    # step4: 4*0.5 + 2*0.5 = 3 ; step5: 5*0.5 + 3*0.5 = 4
    assert abs(ind.ema([1, 2, 3, 4, 5], 3) - 4.0) < 1e-9


def test_rsi_all_gains_is_100():
    closes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
    assert ind.rsi(closes, 14) == 100.0


def test_rsi_midrange():
    # чередование +1/-1 даёт RSI около 50
    closes = [100]
    for i in range(20):
        closes.append(closes[-1] + (1 if i % 2 == 0 else -1))
    r = ind.rsi(closes, 14)
    assert 40 < r < 60


def test_atr_constant_range():
    # свечи с постоянным диапазоном 2 и без гэпов -> ATR = 2
    candles = [_c(100 + i, high=101 + i, low=99 + i) for i in range(30)]
    a = ind.atr(candles, 14)
    assert abs(a - 2.0) < 0.3  # сглаживание Уайлдера, близко к 2


def test_bollinger_symmetry():
    closes = [10] * 20
    lo, mid, up = ind.bollinger(closes, 20, 2.0)
    assert mid == 10.0 and lo == 10.0 and up == 10.0  # нулевая дисперсия


def test_rolling_high_low():
    candles = [_c(5, high=h, low=l) for h, l in [(10, 1), (12, 2), (9, 3), (11, 0)]]
    assert ind.rolling_high(candles, 4) == 12
    assert ind.rolling_low(candles, 4) == 0
    assert ind.rolling_high(candles, 2) == 11  # последние 2


def test_insufficient_data_returns_none():
    assert ind.rsi([1, 2, 3], 14) is None
    assert ind.atr([_c(1)], 14) is None
    assert ind.bollinger([1, 2], 20) is None
