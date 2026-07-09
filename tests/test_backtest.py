"""Проверки самого бэктестера: консервативность модели исполнения и
воспроизводимость sanity-исходов."""
from app.backtest.candle import Candle
from app.backtest.engine import BacktestConfig, BacktestEngine, _check_hard, _fee_aware_levels
from app.engine.models import Side
from app.strategy.sanity import AlwaysLongStrategy, RandomStrategy


def _c(open_time, o, h, l, c, tf_ms=60_000):
    return Candle(open_time, o, h, l, c, 100.0, open_time + tf_ms - 1, 10, 60.0)


def test_both_touched_assumes_sl():
    # long: свеча задевает и TP (high>=tp), и SL (low<=sl) -> берём SL
    c = _c(0, 100, 105, 95, 100)
    hit, price, reason = _check_hard(c, Side.LONG, tp=104, sl=96)
    assert hit and price == 96 and "sl" in reason and "both" in reason


def test_only_tp():
    c = _c(0, 100, 105, 99, 104)
    hit, price, reason = _check_hard(c, Side.LONG, tp=104, sl=96)
    assert hit and price == 104 and reason == "tp"


def test_only_sl():
    c = _c(0, 100, 101, 95, 96)
    hit, price, reason = _check_hard(c, Side.LONG, tp=110, sl=96)
    assert hit and price == 96 and reason == "sl"


def test_short_side_levels_inverted():
    # short: SL выше входа, TP ниже. Свеча пробила только вниз (TP).
    c = _c(0, 100, 101, 95, 96)
    hit, price, reason = _check_hard(c, Side.SHORT, tp=96, sl=104)
    assert hit and price == 96 and reason == "tp"


def test_no_hit():
    c = _c(0, 100, 100.5, 99.5, 100)
    hit, price, reason = _check_hard(c, Side.LONG, tp=104, sl=96)
    assert not hit


def test_fee_aware_levels_long_ordering():
    # sl_pct должен быть заметно выше пола комиссий (0.1%), иначе fee-aware SL
    # садится на цену входа — это корректно, но вырожденно для проверки порядка.
    cfg = BacktestConfig(tp_pct=0.005, sl_pct=0.005)
    tp, sl = _fee_aware_levels(2000.0, Side.LONG, cfg)
    assert tp > 2000 > sl  # TP выше входа, SL ниже


def test_sl_at_fee_floor_collapses_to_entry():
    # осознанная проверка вырожденного случая: sl_pct на уровне пола комиссий ->
    # SL ≈ вход (тебя выбивает одними комиссиями). Важно понимать при выборе sl_pct.
    cfg = BacktestConfig(tp_pct=0.005, sl_pct=0.001, entry_is_maker=False)  # floor=0.1%
    _, sl = _fee_aware_levels(2000.0, Side.LONG, cfg)
    assert abs(sl - 2000.0) < 1.0  # практически на входе


def test_random_not_profitable_on_synthetic_flat_market():
    # плоский рынок из мелких свечей: у random-стратегии не должно быть плюса,
    # должна проигрывать примерно комиссии.
    c1m, c15m = [], []
    price = 1000.0
    for i in range(15 * 200):  # 200 15m-баров
        # крошечный шум
        o = price
        price += (1 if i % 2 == 0 else -1) * 0.1
        h, l, c = max(o, price) + 0.05, min(o, price) - 0.05, price
        c1m.append(_c(i * 60_000, o, h, l, c))
        if (i + 1) % 15 == 0:
            c15m.append(_c((i - 14) * 60_000, o, h, l, c, tf_ms=15 * 60_000))
    cfg = BacktestConfig(tp_pct=0.002, sl_pct=0.001)
    result = BacktestEngine(cfg).run(RandomStrategy(seed=1), {"15m": c15m, "1m": c1m})
    total_net = sum(t.net_pnl for t in result.trades)
    # не прибыльна (нет lookahead), и убыток не абсурдно большой
    assert total_net <= 0


def test_always_long_single_trade_no_levels():
    c1m, c15m = [], []
    for i in range(15 * 10):
        c1m.append(_c(i * 60_000, 100 + i, 100 + i + 0.5, 100 + i - 0.5, 100 + i + 0.2))
        if (i + 1) % 15 == 0:
            c15m.append(_c((i - 14) * 60_000, 100, 200, 90, 100 + i, tf_ms=15 * 60_000))
    cfg = BacktestConfig(tp_pct=0.0, sl_pct=0.0)  # без выходов -> одна сделка на весь период
    result = BacktestEngine(cfg).run(AlwaysLongStrategy(), {"15m": c15m, "1m": c1m})
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "end_of_data"


def test_entry_reason_threaded_from_decision():
    # Decision.reason из strategy.decide() должен долетать до Trade.entry_reason
    # (иначе теги вроде тех, что ставит RegimeRouterStrategy, невозможно
    # увидеть даже в JSON-ответе бэктеста).
    c1m, c15m = [], []
    for i in range(15 * 10):
        c1m.append(_c(i * 60_000, 100 + i, 100 + i + 0.5, 100 + i - 0.5, 100 + i + 0.2))
        if (i + 1) % 15 == 0:
            c15m.append(_c((i - 14) * 60_000, 100, 200, 90, 100 + i, tf_ms=15 * 60_000))
    cfg = BacktestConfig(tp_pct=0.0, sl_pct=0.0)
    result = BacktestEngine(cfg).run(AlwaysLongStrategy(), {"15m": c15m, "1m": c1m})
    assert result.trades[0].entry_reason == "always_long"
