from __future__ import annotations
from decimal import Decimal

from app.engine.models import Side


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def solve_exit_price_for_net_pnl(
    entry_price: Decimal | float | str,
    qty: Decimal | float | str,
    entry_fee: Decimal | float | str,
    exit_fee_rate: Decimal | float | str,
    target_net_pnl: Decimal | float | str,
    side: Side,
) -> Decimal:
    """
    Цена выхода P, при которой net_pnl (после ОБЕИХ комиссий — фактической
    комиссии входа и оценочной комиссии выхода по exit_fee_rate) равен
    ровно target_net_pnl.

    net_pnl = gross_pnl(P) - entry_fee - exit_notional(P) * exit_fee_rate

    target_net_pnl > 0 для TP (желаемая чистая прибыль),
    target_net_pnl < 0 для SL (допустимый чистый убыток).

    LONG:  P = (target + entry_price*qty + entry_fee) / (qty * (1 - exit_fee_rate))
    SHORT: P = (entry_price*qty - entry_fee - target)  / (qty * (1 + exit_fee_rate))

    При entry_fee=0, exit_fee_rate=0 сводится к P = entry_price * (1 + target/notional) —
    то есть к прежней "наивной" процентной формуле, так что это строгое обобщение,
    не смена поведения при отсутствии данных о комиссиях.
    """
    ep, q, fee, rate, target = (
        _d(entry_price), _d(qty), _d(entry_fee), _d(exit_fee_rate), _d(target_net_pnl),
    )
    if q <= 0:
        raise ValueError("qty must be positive")

    if side == Side.LONG:
        return (target + ep * q + fee) / (q * (1 - rate))
    elif side == Side.SHORT:
        return (ep * q - fee - target) / (q * (1 + rate))
    raise ValueError(f"unsupported side for exit price calc: {side}")
