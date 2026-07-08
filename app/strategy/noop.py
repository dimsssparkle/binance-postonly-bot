from __future__ import annotations

from app.strategy.base import Action, Decision, PositionView, Strategy
from app.strategy.market_view import MarketView


class NoopStrategy(Strategy):
    """Заглушка — всегда HOLD, никогда сама не торгует. Бот до готовности
    реальной стратегии управляется только вручную (/trade/manual, /trade/close),
    но уже через тот же путь decide()->runner->handle_signal, что и боевая
    стратегия."""

    def decide(self, market: MarketView, position: PositionView) -> Decision:
        return Decision(Action.HOLD, reason="noop")
