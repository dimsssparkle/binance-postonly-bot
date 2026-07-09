from app.strategy.params import ParamSpec, ParamType, validate_params


def test_int_range_ok():
    spec = ParamSpec("lookback", ParamType.INT, 20, "Окно", min=5, max=200)
    assert spec.validate(50) == 50


def test_int_below_min_rejected():
    spec = ParamSpec("lookback", ParamType.INT, 20, "Окно", min=5, max=200)
    try:
        spec.validate(1)
        assert False, "should have raised"
    except ValueError:
        pass


def test_int_above_max_rejected():
    spec = ParamSpec("lookback", ParamType.INT, 20, "Окно", min=5, max=200)
    try:
        spec.validate(500)
        assert False, "should have raised"
    except ValueError:
        pass


def test_float_range_ok():
    spec = ParamSpec("flow_long", ParamType.FLOAT, 0.55, "Поток", min=0.5, max=1.0)
    assert spec.validate("0.7") == 0.7


def test_bool_accepts_native_and_string_forms():
    spec = ParamSpec("flow_filter", ParamType.BOOL, True, "Фильтр")
    assert spec.validate(True) is True
    assert spec.validate(False) is False
    assert spec.validate("true") is True
    assert spec.validate("false") is False
    assert spec.validate("1") is True
    assert spec.validate("0") is False


def test_bool_rejects_garbage_string():
    spec = ParamSpec("flow_filter", ParamType.BOOL, True, "Фильтр")
    try:
        spec.validate("maybe")
        assert False, "should have raised"
    except ValueError:
        pass


def test_enum_accepts_valid_choice():
    spec = ParamSpec("tf", ParamType.ENUM, "15m", "Таймфрейм", choices=["1m", "15m", "1h"])
    assert spec.validate("1h") == "1h"


def test_enum_rejects_invalid_choice():
    spec = ParamSpec("tf", ParamType.ENUM, "15m", "Таймфрейм", choices=["1m", "15m", "1h"])
    try:
        spec.validate("3d")
        assert False, "should have raised"
    except ValueError:
        pass


def test_validate_params_unknown_key_rejected():
    specs = [ParamSpec("lookback", ParamType.INT, 20, "Окно", min=5, max=200)]
    try:
        validate_params(specs, {"lookback": 30, "bogus": 1})
        assert False, "should have raised"
    except ValueError:
        pass


def test_validate_params_missing_key_falls_back_to_default():
    specs = [ParamSpec("lookback", ParamType.INT, 20, "Окно", min=5, max=200)]
    assert validate_params(specs, {}) == {"lookback": 20}


def test_validate_params_full_round_trip():
    specs = [
        ParamSpec("lookback", ParamType.INT, 20, "Окно", min=5, max=200),
        ParamSpec("flow_long", ParamType.FLOAT, 0.55, "Поток лонг", min=0.5, max=1.0),
        ParamSpec("flow_short", ParamType.FLOAT, 0.45, "Поток шорт", min=0.0, max=0.5),
        ParamSpec("tf", ParamType.ENUM, "15m", "Таймфрейм", choices=["1m", "15m"]),
    ]
    raw = {"lookback": 30, "flow_long": 0.6, "flow_short": 0.4, "tf": "1m"}
    assert validate_params(specs, raw) == raw
