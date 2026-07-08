from __future__ import annotations
from typing import Any, Dict, List


class LeverageBracketCache:
    """Кэширует тиры maintenance margin по символу — нужны только для ОЦЕНКИ
    цены ликвидации гипотетической новой позиции на дашборде (реальная
    liquidationPrice уже открытой позиции приходит от Binance готовой через
    get_position_risk, тут не участвует). Тиры меняются редко, кэшируем на
    весь процесс, как CommissionRateCache/SymbolFilterCache."""

    def __init__(self, rest_client) -> None:
        self._client = rest_client
        self._brackets: Dict[str, List[Dict[str, Any]]] = {}

    def get(self, symbol: str) -> List[Dict[str, Any]]:
        sym = symbol.upper()
        if sym not in self._brackets:
            raw = self._client.get_leverage_brackets(sym)
            self._brackets[sym] = [
                {
                    "notionalFloor": float(b["notionalFloor"]),
                    "notionalCap": float(b["notionalCap"]),
                    "maintMarginRatio": float(b["maintMarginRatio"]),
                }
                for b in raw
            ]
        return self._brackets[sym]
