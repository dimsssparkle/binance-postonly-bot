"""Momentum (пробойная) стратегия на 15m, подтверждённая order-flow.

Гипотеза: пробой N-барового экстремума на фоне агрессивного потока в ту же
сторону имеет краткосрочное продолжение. Few-parameter намеренно — чем
меньше ручек, тем меньше риск переобучения на истории.

Вход-only: выходы делает fee-aware TP/SL (fixed-режим) или strategy EXIT в
dynamic-режиме позже. Пока стратегия про выход не думает.
"""
from __future__ import annotations

from app.strategy.base import Action, Decision, PositionView, Strategy
from app.strategy.indicators import rolling_high, rolling_low
from app.strategy.market_view import MarketView
from app.engine.models import Side


class MomentumStrategy(Strategy):
    def __init__(self, lookback: int = 20, flow_long: float = 0.55,
                 flow_short: float = 0.45, tf: str = "15m") -> None:
        self.lookback = lookback
        self.flow_long = flow_long      # мин. доля агрессивных покупок для лонга
        self.flow_short = flow_short    # макс. доля для шорта (т.е. давление продавцов)
        self.tf = tf

    def decide(self, market: MarketView, position: PositionView) -> Decision:
        if position.side != Side.FLAT:
            return Decision(Action.HOLD)
        cs = market.candles(self.tf, self.lookback + 1)
        if len(cs) < self.lookback + 1:
            return Decision(Action.HOLD, "warmup")
        cur = cs[-1]
        prior = cs[:-1]  # ровно lookback свечей ДО текущей
        hi = rolling_high(prior, self.lookback)
        lo = rolling_low(prior, self.lookback)
        f = cur.taker_buy_fraction

        if hi is not None and cur.close > hi and f >= self.flow_long:
            return Decision(Action.ENTER_LONG, f"breakout>{hi:.2f} flow={f:.2f}")
        if lo is not None and cur.close < lo and f <= self.flow_short:
            return Decision(Action.ENTER_SHORT, f"breakdown<{lo:.2f} flow={f:.2f}")
        return Decision(Action.HOLD)
