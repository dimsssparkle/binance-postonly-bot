from app.backtest.candle import Candle
from app.strategy.preview import compute_preview


def _c(i, close, high=None, low=None):
    high = high if high is not None else close
    low = low if low is not None else close
    return Candle(i * 60_000, close, high, low, close, 100.0, i * 60_000 + 59_999, 10, 60.0)


def test_momentum_price_channel_shape():
    candles = [_c(i, 100 + i, high=101 + i, low=99 + i) for i in range(60)]
    result = compute_preview("momentum", {"lookback": 20}, candles, limit=30)
    assert result["kind"] == "price_channel"
    upper, lower = result["series"]["upper"], result["series"]["lower"]
    assert len(upper) > 0 and len(lower) > 0
    assert len(upper) <= 30
    assert upper[-1]["value"] >= upper[0]["value"]  # монотонный тренд -> канал растёт
    assert all(u["value"] >= l["value"] for u, l in zip(upper, lower))


def test_mean_reversion_oscillator_shape():
    candles = [_c(i, 100 + (i % 5)) for i in range(60)]
    result = compute_preview(
        "mean_reversion", {"period": 14, "oversold": 25.0, "overbought": 75.0}, candles, limit=30)
    assert result["kind"] == "oscillator"
    assert "rsi" in result["series"]
    assert result["thresholds"] == {"oversold": 25.0, "overbought": 75.0}
    assert all(0 <= p["value"] <= 100 for p in result["series"]["rsi"])


def test_regime_router_oscillator_shape():
    candles = [_c(i, 100 + i, high=101 + i, low=99 + i) for i in range(60)]
    result = compute_preview(
        "regime_router", {"adx_period": 14, "adx_threshold": 30.0}, candles, limit=30)
    assert result["kind"] == "oscillator"
    assert "adx" in result["series"]
    assert result["thresholds"] == {"adx_threshold": 30.0}


def test_unknown_strategy_raises():
    try:
        compute_preview("bogus", {}, [], limit=10)
        assert False, "should have raised"
    except ValueError:
        pass


def test_limit_caps_returned_points():
    candles = [_c(i, 100 + i, high=101 + i, low=99 + i) for i in range(200)]
    result = compute_preview("momentum", {"lookback": 20}, candles, limit=10)
    assert len(result["series"]["upper"]) == 10


def test_defaults_used_when_params_missing():
    candles = [_c(i, 100 + i, high=101 + i, low=99 + i) for i in range(60)]
    result = compute_preview("momentum", {}, candles, limit=30)
    assert result["kind"] == "price_channel"
    assert len(result["series"]["upper"]) > 0
