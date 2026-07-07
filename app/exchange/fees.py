from __future__ import annotations
from typing import Dict


class CommissionRateCache:
    """Кэширует maker/takerCommissionRate по символу — меняются редко
    (привязаны к VIP-тиру аккаунта по 30-дневному объёму), не нужно
    дёргать REST на каждый расчёт TP/SL."""

    def __init__(self, rest_client) -> None:
        self._client = rest_client
        self._rates: Dict[str, Dict[str, float]] = {}

    def get(self, symbol: str) -> Dict[str, float]:
        sym = symbol.upper()
        if sym not in self._rates:
            resp = self._client.get_commission_rate(sym)
            self._rates[sym] = {
                "maker": float(resp.get("makerCommissionRate", 0.0)),
                "taker": float(resp.get("takerCommissionRate", 0.0)),
            }
        return self._rates[sym]

    def refresh(self) -> None:
        self._rates.clear()
