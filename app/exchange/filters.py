from __future__ import annotations
from typing import Any, Dict


def parse_symbol_filters(exchange_info: Dict[str, Any], symbol: str) -> Dict[str, str]:
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


class SymbolFilterCache:
    """Кэширует exchangeInfo/filters по символу, чтобы не дёргать REST на каждый вызов."""

    def __init__(self, rest_client) -> None:
        self._client = rest_client
        self._exchange_info: Dict[str, Any] | None = None
        self._filters: Dict[str, Dict[str, str]] = {}

    def _ensure_exchange_info(self) -> Dict[str, Any]:
        if self._exchange_info is None:
            self._exchange_info = self._client.exchange_info()
        return self._exchange_info

    def get(self, symbol: str) -> Dict[str, str]:
        sym = symbol.upper()
        if sym not in self._filters:
            info = self._ensure_exchange_info()
            self._filters[sym] = parse_symbol_filters(info, sym)
        return self._filters[sym]
