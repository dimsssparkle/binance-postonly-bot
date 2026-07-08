"""Стратегии с ИЗВЕСТНЫМ исходом — проверяют сам бэктестер, а не рынок.

Если бэктестер корректен:
  - AlwaysLong (без TP/SL, держит весь период) ≈ buy&hold минус один round-trip.
  - Random (случайное направление, симметричные TP/SL) ≈ −(комиссии × сделки):
    edge нет, платим только комиссии. Если случайная стратегия «прибыльна» —
    в бэктестере lookahead, и НИ ОДНОМУ результату доверять нельзя.
"""
from __future__ import annotations
import random

from app.engine.models import Side
from app.strategy.base import Action, Decision, PositionView, Strategy
from app.strategy.market_view import MarketView


class AlwaysLongStrategy(Strategy):
    """Войти в лонг и держать. С конфигом tp_pct=sl_pct=0 -> одна сделка на весь
    период -> должно совпасть с buy&hold минус комиссии."""

    def decide(self, market: MarketView, position: PositionView) -> Decision:
        if position.side == Side.FLAT:
            return Decision(Action.ENTER_LONG, "always_long")
        return Decision(Action.HOLD)


class RandomStrategy(Strategy):
    """Случайное направление на каждом входе. Детерминирована сидом."""

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)

    def decide(self, market: MarketView, position: PositionView) -> Decision:
        if position.side == Side.FLAT:
            if self._rng.random() < 0.5:
                return Decision(Action.ENTER_LONG, "random")
            return Decision(Action.ENTER_SHORT, "random")
        return Decision(Action.HOLD)
