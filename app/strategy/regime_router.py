"""Regime Router — авто-переключение между стратегиями по силе тренда (ADX).

Композитная Strategy: классифицирует режим рынка из market (тренд/флэт)
и целиком делегирует решение подходящему кандидату. Сама по себе не
торгует — вся торговая логика остаётся в trending_strategy/ranging_strategy.
decide() чистый и без побочных эффектов, как и требует контракт Strategy,
поэтому BacktestEngine работает с роутером без единого изменения движка.

Известное ограничение (осознанно отложено): роутер переклассифицирует
режим на КАЖДОМ вызове decide(), включая когда позиция уже открыта. Сейчас
это безопасно — оба кандидата (momentum/mean_reversion) отвечают HOLD, если
position.side != FLAT, так что смена режима в середине сделки просто
спросит кандидата, который всё равно скажет HOLD. Если у кандидата
появится динамический EXIT, смена режима в середине сделки будет спрашивать
ДРУГОГО кандидата, открывшего ли он эту позицию — семантически спорно.
Правильный fix тогда: не мьютить состояние внутри роутера (сломает чистоту
и не переживёт рестарт бота), а прокинуть непрозрачный PositionView.strategy_tag,
чтобы роутер мог опознать "чья" это позиция, оставаясь без состояния.
"""
from __future__ import annotations

from app.strategy.base import Action, Decision, PositionView, Strategy
from app.strategy.indicators import adx
from app.strategy.market_view import MarketView
from app.engine.models import Side

REGIME_TRENDING = "trending"
REGIME_RANGING = "ranging"


class RegimeRouterStrategy(Strategy):
    def __init__(self, trending_strategy: Strategy, ranging_strategy: Strategy,
                 adx_period: int = 14, adx_threshold: float = 25.0, tf: str = "15m") -> None:
        self.trending_strategy = trending_strategy
        self.ranging_strategy = ranging_strategy
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.tf = tf

    def _classify(self, market: MarketView):
        candles = market.candles(self.tf, self.adx_period * 4)
        a = adx(candles, self.adx_period)
        if a is None:
            return None, None
        return (REGIME_TRENDING if a >= self.adx_threshold else REGIME_RANGING), a

    def decide(self, market: MarketView, position: PositionView) -> Decision:
        regime, a_val = self._classify(market)
        if regime is None:
            return Decision(Action.HOLD, "regime=warmup")
        candidate = self.trending_strategy if regime == REGIME_TRENDING else self.ranging_strategy
        inner = candidate.decide(market, position)
        tag = f"regime={regime}(adx={a_val:.1f}) via {type(candidate).__name__}"
        return Decision(inner.action, f"{tag}: {inner.reason}" if inner.reason else tag)
