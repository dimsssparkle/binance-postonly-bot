from __future__ import annotations
from decimal import Decimal, ROUND_DOWN


def d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def round_to_step(value: float | str, step: float | str) -> str:
    """Жёсткое округление вниз к шагу (tickSize/stepSize). Возвращает строку."""
    v, s = d(value), d(step)
    if s == 0:
        return str(v)
    quant = (d(1) / s).quantize(d(1), rounding=ROUND_DOWN)
    return str((v * quant).to_integral_value(rounding=ROUND_DOWN) / quant)


def round_up_to_step(value: float | str, step: float | str) -> str:
    """Жёсткое округление ВВЕРХ к шагу (используем для соблюдения MIN_NOTIONAL)."""
    v, s = d(value), d(step)
    if s == 0:
        return str(v)
    quant = (d(1) / s).quantize(d(1), rounding=ROUND_DOWN)
    scaled = v * quant
    floored = scaled.to_integral_value(rounding=ROUND_DOWN)
    if scaled != floored:
        floored += 1
    return str(floored / quant)
