from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    """Одна закрытая свеча + признаки order-flow, которые реально доступны в
    исторических klines Binance (проверено вживую). Общий тип для live и
    бэктеста — стратегия видит ровно этот объект в обоих режимах.

    frozen=True специально: свеча — это факт прошлого, её нельзя менять.
    Никакой мутации = никакого случайного repainting на уровне данных.
    """
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int
    num_trades: int
    taker_buy_base: float

    @property
    def taker_buy_fraction(self) -> float:
        """Доля агрессивных покупок в объёме свечи (0..1). Прокси order-flow:
        >0.5 — давление покупателей, <0.5 — продавцов. 0.5 при нулевом объёме."""
        return (self.taker_buy_base / self.volume) if self.volume > 0 else 0.5

    @classmethod
    def from_binance_kline(cls, k: list) -> "Candle":
        """Из сырого массива klines Binance (12 полей).
        [0]openTime [1]O [2]H [3]L [4]C [5]vol [6]closeTime
        [7]quoteVol [8]numTrades [9]takerBuyBase [10]takerBuyQuote [11]ignore
        """
        return cls(
            open_time_ms=int(k[0]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
            close_time_ms=int(k[6]),
            num_trades=int(k[8]),
            taker_buy_base=float(k[9]),
        )
