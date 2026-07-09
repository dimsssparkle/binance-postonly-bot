from app.strategy.mean_reversion import MeanReversionStrategy
from app.strategy.momentum import MomentumStrategy
from app.strategy.regime_router import RegimeRouterStrategy
from app.strategy.registry import STRATEGY_REGISTRY, build_strategy


def test_registry_contains_expected_keys():
    assert set(STRATEGY_REGISTRY) == {"momentum", "mean_reversion", "regime_router"}


def test_sanity_and_noop_strategies_excluded():
    registered_classes = {meta.cls for meta in STRATEGY_REGISTRY.values()}
    from app.strategy.noop import NoopStrategy
    from app.strategy.sanity import AlwaysLongStrategy, RandomStrategy
    assert AlwaysLongStrategy not in registered_classes
    assert RandomStrategy not in registered_classes
    assert NoopStrategy not in registered_classes


def test_build_strategy_momentum_defaults():
    s = build_strategy("momentum", {})
    assert isinstance(s, MomentumStrategy)
    assert s.lookback == 20
    assert s.tf == "15m"


def test_build_strategy_momentum_overrides():
    s = build_strategy("momentum", {"lookback": 30, "tf": "1h"})
    assert s.lookback == 30
    assert s.tf == "1h"


def test_build_strategy_mean_reversion_defaults():
    s = build_strategy("mean_reversion", {})
    assert isinstance(s, MeanReversionStrategy)
    assert s.period == 14
    assert s.flow_filter is True


def test_build_strategy_unknown_key_raises():
    try:
        build_strategy("bogus", {})
        assert False, "should have raised"
    except ValueError:
        pass


def test_build_strategy_invalid_param_raises():
    try:
        build_strategy("momentum", {"lookback": 1})  # below min=5
        assert False, "should have raised"
    except ValueError:
        pass


def test_build_strategy_regime_router_without_sub_strategies_raises():
    try:
        build_strategy("regime_router", {"trending_config_id": 1, "ranging_config_id": 2})
        assert False, "should have raised"
    except ValueError:
        pass


def test_build_strategy_regime_router_wires_resolved_candidates():
    trending = build_strategy("momentum", {})
    ranging = build_strategy("mean_reversion", {})
    router = build_strategy(
        "regime_router", {"adx_threshold": 30.0, "trending_config_id": 1, "ranging_config_id": 2},
        sub_strategies={"trending_config_id": trending, "ranging_config_id": ranging},
    )
    assert isinstance(router, RegimeRouterStrategy)
    assert router.trending_strategy is trending
    assert router.ranging_strategy is ranging
    assert router.adx_threshold == 30.0


def test_build_strategy_regime_router_missing_sub_strategy_key_raises():
    trending = build_strategy("momentum", {})
    try:
        build_strategy(
            "regime_router", {"trending_config_id": 1, "ranging_config_id": 2},
            sub_strategies={"trending_config_id": trending},  # ranging_config_id отсутствует
        )
        assert False, "should have raised"
    except ValueError:
        pass
