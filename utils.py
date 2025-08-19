from __future__ import annotations
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any

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


def parse_symbol_filters(exchange_info: Dict[str, Any], symbol: str):
    """Достаёт tickSize, stepSize и minNotional для symbol из futures exchangeInfo."""
    sym = next((s for s in exchange_info["symbols"] if s["symbol"] == symbol), None)
    if not sym:
        raise ValueError(f"Symbol {symbol} not found in exchangeInfo")
    tick_size = "0.01"
    step_size = "0.001"
    min_notional = "5"
    for f in sym["filters"]:
        if f["filterType"] == "PRICE_FILTER":
            tick_size = f["tickSize"]
        elif f["filterType"] == "LOT_SIZE":
            step_size = f["stepSize"]
        elif f["filterType"] == "MIN_NOTIONAL":
            min_notional = f["notional"] if "notional" in f else f["minNotional"]
    return {
        "tickSize": tick_size,
        "stepSize": step_size,
        "minNotional": min_notional,
    }
