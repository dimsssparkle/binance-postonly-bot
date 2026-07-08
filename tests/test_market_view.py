"""MarketView НИКОГДА не отдаёт свечу из будущего — структурная гарантия
против repainting. Если этот тест падает, вся честность бэктеста под вопросом.
"""
from app.backtest.candle import Candle
from app.strategy.market_view import MarketView


def _candle(open_time_ms: int, close: float, tf_ms: int = 60_000) -> Candle:
    return Candle(
        open_time_ms=open_time_ms, open=close, high=close, low=close, close=close,
        volume=100.0, close_time_ms=open_time_ms + tf_ms - 1, num_trades=10, taker_buy_base=60.0,
    )


def test_future_candles_excluded():
    # 5 свечей по 1м; now_ms на границе 3-й свечи -> видно только 3
    candles = [_candle(i * 60_000, close=100 + i) for i in range(5)]
    now = candles[2].close_time_ms  # ровно закрытие 3-й
    mv = MarketView({"1m": candles}, now_ms=now)
    visible = mv.candles("1m")
    assert len(visible) == 3
    assert all(c.close_time_ms <= now for c in visible)
    # 4-я и 5-я (будущее) не видны
    assert visible[-1].close == 102


def test_last_price_is_latest_visible():
    candles = [_candle(i * 60_000, close=100 + i) for i in range(5)]
    now = candles[3].close_time_ms
    mv = MarketView({"1m": candles}, now_ms=now)
    assert mv.last_price("1m") == 103


def test_multi_timeframe_independent_cutoff():
    c1m = [_candle(i * 60_000, close=i, tf_ms=60_000) for i in range(30)]
    c15m = [_candle(i * 900_000, close=i * 10, tf_ms=900_000) for i in range(3)]
    now = 900_000 * 1 + 900_000 - 1  # закрытие 2-й 15m-свечи
    mv = MarketView({"1m": c1m, "15m": c15m}, now_ms=now)
    assert len(mv.candles("15m")) == 2  # только 2 закрытые 15m
    assert all(c.close_time_ms <= now for c in mv.candles("1m"))


def test_n_slice_returns_most_recent():
    candles = [_candle(i * 60_000, close=i) for i in range(10)]
    now = candles[-1].close_time_ms
    mv = MarketView({"1m": candles}, now_ms=now)
    last3 = mv.candles("1m", 3)
    assert [c.close for c in last3] == [7, 8, 9]


def test_empty_timeframe():
    mv = MarketView({}, now_ms=1000)
    assert mv.candles("1m") == []
    assert mv.last("1m") is None
    assert mv.last_price("1m") is None
