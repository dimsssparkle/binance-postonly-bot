"""Формализация fee-aware формулы TP/SL, уже подтверждённой вживую на реальном
счёте (вход 1794.23, комиссия 0.01076538 -> TP 1800.15 / SL 1793.33)."""
from decimal import Decimal

from app.engine.fees import solve_exit_price_for_net_pnl
from app.engine.models import Side
from app.engine.rounding import round_to_step


def test_reproduces_live_observed_prices():
    entry_price = 1794.23
    qty = 0.012
    entry_fee = Decimal("0.01076538")
    taker_rate = 0.0005
    notional = entry_price * qty
    tp = solve_exit_price_for_net_pnl(entry_price, qty, entry_fee, taker_rate,
                                      notional * 0.0023, Side.LONG)
    sl = solve_exit_price_for_net_pnl(entry_price, qty, entry_fee, taker_rate,
                                      -(notional * 0.0015), Side.LONG)
    assert round_to_step(tp, "0.01") == "1800.15"
    assert round_to_step(sl, "0.01") == "1793.33"


def test_net_pnl_at_computed_tp_matches_target():
    entry_price, qty = Decimal("1800"), Decimal("0.01")
    entry_fee, taker = Decimal("0.0036"), Decimal("0.0005")
    target = Decimal("0.05")
    tp = solve_exit_price_for_net_pnl(entry_price, qty, entry_fee, taker, target, Side.LONG)
    gross = (tp - entry_price) * qty
    exit_fee = tp * qty * taker
    net = gross - entry_fee - exit_fee
    assert abs(net - target) < Decimal("1e-9")


def test_zero_fees_reduces_to_naive_percent():
    # при нулевых комиссиях цена выхода = наивная % от входа
    entry_price, qty = Decimal("1000"), Decimal("1")
    target = entry_price * qty * Decimal("0.01")  # 1% нотионала
    tp = solve_exit_price_for_net_pnl(entry_price, qty, Decimal("0"), Decimal("0"), target, Side.LONG)
    assert abs(tp - Decimal("1010")) < Decimal("1e-9")


def test_short_side_direction():
    # для SHORT TP ниже входа
    entry_price, qty = Decimal("2000"), Decimal("0.01")
    target = entry_price * qty * Decimal("0.002")
    tp = solve_exit_price_for_net_pnl(entry_price, qty, Decimal("0"), Decimal("0.0005"), target, Side.SHORT)
    assert tp < entry_price
