from app.backtest.candle import Candle
from app.engine.models import Side
from app.strategy.base import Action, Decision, PositionView, Strategy
from app.strategy.market_view import MarketView
from app.strategy.mean_reversion import MeanReversionStrategy
from app.strategy.momentum import MomentumStrategy
from app.strategy.regime_router import RegimeRouterStrategy
import app.strategy.regime_router as regime_router_module


def _c(close, high=None, low=None):
    high = high if high is not None else close
    low = low if low is not None else close
    return Candle(0, close, high, low, close, 100.0, 0, 10, 60.0)


class _FixedStrategy(Strategy):
    """Стратегия-заглушка, всегда отдающая заранее заданное решение —
    для изоляции логики роутера от логики реальных кандидатов."""

    def __init__(self, decision: Decision):
        self._decision = decision

    def decide(self, market: MarketView, position: PositionView) -> Decision:
        return self._decision


def _market_with_n_candles(n: int, tf: str = "15m") -> MarketView:
    candles = [_c(100 + i) for i in range(n)]
    return MarketView({tf: candles}, now_ms=candles[-1].close_time_ms if candles else 0)


def test_self_check_same_instance_both_regimes_matches_direct_call():
    # Мирит калибровочный дух sanity.py: если оба "кандидата" — один и тот
    # же объект, роутер должен вести себя ИДЕНТИЧНО прямому вызову этого
    # объекта, независимо от того, что скажет ADX.
    inner_decision = Decision(Action.ENTER_LONG, "always long")
    same = _FixedStrategy(inner_decision)
    router = RegimeRouterStrategy(same, same, adx_period=14, adx_threshold=25.0)
    market = _market_with_n_candles(200)
    position = PositionView(Side.FLAT)

    router_result = router.decide(market, position)
    direct_result = same.decide(market, position)
    assert router_result.action == direct_result.action


def test_warmup_returns_hold_without_calling_candidates(monkeypatch):
    calls = []

    class _Tracking(Strategy):
        def decide(self, market, position):
            calls.append(self)
            return Decision(Action.ENTER_LONG, "should not be called")

    monkeypatch.setattr(regime_router_module, "adx", lambda candles, period: None)
    router = RegimeRouterStrategy(_Tracking(), _Tracking())
    result = router.decide(_market_with_n_candles(5), PositionView(Side.FLAT))
    assert result.action == Action.HOLD
    assert result.reason == "regime=warmup"
    assert calls == []


def test_adx_above_threshold_delegates_to_trending(monkeypatch):
    monkeypatch.setattr(regime_router_module, "adx", lambda candles, period: 40.0)
    trending = _FixedStrategy(Decision(Action.ENTER_LONG, "trend signal"))
    ranging = _FixedStrategy(Decision(Action.ENTER_SHORT, "range signal"))
    router = RegimeRouterStrategy(trending, ranging, adx_threshold=25.0)
    result = router.decide(_market_with_n_candles(200), PositionView(Side.FLAT))
    assert result.action == Action.ENTER_LONG
    assert "regime=trending" in result.reason
    assert "trend signal" in result.reason


def test_adx_below_threshold_delegates_to_ranging(monkeypatch):
    monkeypatch.setattr(regime_router_module, "adx", lambda candles, period: 10.0)
    trending = _FixedStrategy(Decision(Action.ENTER_LONG, "trend signal"))
    ranging = _FixedStrategy(Decision(Action.ENTER_SHORT, "range signal"))
    router = RegimeRouterStrategy(trending, ranging, adx_threshold=25.0)
    result = router.decide(_market_with_n_candles(200), PositionView(Side.FLAT))
    assert result.action == Action.ENTER_SHORT
    assert "regime=ranging" in result.reason
    assert "range signal" in result.reason


def test_reason_tag_includes_adx_value(monkeypatch):
    monkeypatch.setattr(regime_router_module, "adx", lambda candles, period: 31.2345)
    router = RegimeRouterStrategy(
        _FixedStrategy(Decision(Action.HOLD, "")), _FixedStrategy(Decision(Action.HOLD, "")),
        adx_threshold=25.0)
    result = router.decide(_market_with_n_candles(200), PositionView(Side.FLAT))
    assert "adx=31.2" in result.reason


def test_real_candidates_monotonic_trend_selects_momentum():
    # Реальный интеграционный смоук: без мока adx(), настоящие
    # MomentumStrategy/MeanReversionStrategy на монотонном тренде
    # (тот же фикстур-паттерн, что дал ADX=100 в test_indicators.py).
    candles = [_c(100 + i, high=101 + i, low=99 + i) for i in range(60)]
    market = MarketView({"15m": candles}, now_ms=candles[-1].close_time_ms)
    router = RegimeRouterStrategy(
        MomentumStrategy(lookback=20, tf="15m"),
        MeanReversionStrategy(period=14, tf="15m"),
        adx_period=14, adx_threshold=25.0, tf="15m",
    )
    result = router.decide(market, PositionView(Side.FLAT))
    assert "via MomentumStrategy" in result.reason


def test_real_candidates_choppy_market_selects_mean_reversion():
    closes = [100]
    for i in range(60):
        closes.append(closes[-1] + (1 if i % 2 == 0 else -1))
    candles = [_c(c, high=c + 0.5, low=c - 0.5) for c in closes]
    market = MarketView({"15m": candles}, now_ms=candles[-1].close_time_ms)
    router = RegimeRouterStrategy(
        MomentumStrategy(lookback=20, tf="15m"),
        MeanReversionStrategy(period=14, tf="15m"),
        adx_period=14, adx_threshold=25.0, tf="15m",
    )
    result = router.decide(market, PositionView(Side.FLAT))
    assert "via MeanReversionStrategy" in result.reason
