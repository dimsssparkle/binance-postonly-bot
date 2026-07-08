"""Mean-reversion стратегия на 15m: вход против движения на RSI-экстремумах,
с flow-фильтром, чтобы не ловить нож при экстремальном одностороннем потоке.

Гипотеза: перепроданность/перекупленность на 15m склонна к откату. Few-parameter.
Вход-only (выходы — fee-aware TP/SL или strategy EXIT позже).
"""
from __future__ import annotations

from app.strategy.base import Action, Decision, PositionView, Strategy
from app.strategy.indicators import rsi
from app.strategy.market_view import MarketView
from app.engine.models import Side


class MeanReversionStrategy(Strategy):
    def __init__(self, period: int = 14, oversold: float = 30.0,
                 overbought: float = 70.0, flow_filter: bool = True,
                 tf: str = "15m") -> None:
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.flow_filter = flow_filter
        self.tf = tf

    def decide(self, market: MarketView, position: PositionView) -> Decision:
        if position.side != Side.FLAT:
            return Decision(Action.HOLD)
        closes = market.closes(self.tf, self.period * 4)
        if len(closes) < self.period + 1:
            return Decision(Action.HOLD, "warmup")
        r = rsi(closes, self.period)
        cur = market.last(self.tf)
        if r is None or cur is None:
            return Decision(Action.HOLD)
        f = cur.taker_buy_fraction

        # oversold -> ждём откат вверх; flow-фильтр: покупатели не полностью выбиты
        if r <= self.oversold and (not self.flow_filter or f >= 0.45):
            return Decision(Action.ENTER_LONG, f"rsi={r:.0f}<=os flow={f:.2f}")
        # overbought -> ждём откат вниз
        if r >= self.overbought and (not self.flow_filter or f <= 0.55):
            return Decision(Action.ENTER_SHORT, f"rsi={r:.0f}>=ob flow={f:.2f}")
        return Decision(Action.HOLD)
