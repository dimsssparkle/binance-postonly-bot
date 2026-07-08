from __future__ import annotations
import asyncio
import logging
from typing import Optional

from app.engine.exceptions import EngineBusyError
from app.engine.models import Side
from app.engine.state_machine import ExecutionEngine
from app.strategy.base import Action, PositionView, Strategy
from app.strategy.market_view import MarketView

log = logging.getLogger("strategy_runner")


class StrategyRunner:
    """
    Периодически опрашивает Strategy.decide() и транслирует решение в
    ExecutionEngine.handle_signal — тем же путём, что и ручной /trade/manual.

    NB (Phase 2.1): пока это минимальная версия — строит ПУСТОЙ MarketView и
    FLAT-позицию, поэтому боевая логика не запускается (NoopStrategy всегда
    HOLD). Полноценный candle-aware runner (сбор klines в MarketView, реальная
    PositionView, динамический выход по 1m) — это Phase 2.4, после того как
    стратегия пройдёт валидацию бэктестом. До тех пор live-поведение не
    меняется: бот управляется только вручную.
    """

    def __init__(self, strategy: Strategy, engine: ExecutionEngine, interval_sec: float = 5.0) -> None:
        self.strategy = strategy
        self.engine = engine
        self.interval_sec = interval_sec
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        log.info(f"StrategyRunner started ({type(self.strategy).__name__}, every {self.interval_sec}s)")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()

    def _build_market(self) -> MarketView:
        # Phase 2.4 наполнит это реальными klines. Пока — пусто.
        return MarketView(candles_by_tf={}, now_ms=self.engine.rest.now_ms())

    def _build_position(self) -> PositionView:
        return PositionView(side=Side.FLAT)

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                market = self._build_market()
                position = self._build_position()
                decision = self.strategy.decide(market, position)
                if decision.action in (Action.ENTER_LONG, Action.ENTER_SHORT):
                    side = Side.LONG if decision.action == Action.ENTER_LONG else Side.SHORT
                    log.info(f"[STRATEGY] {decision.action.value} ({decision.reason})")
                    await self.engine.handle_signal(side)
                elif decision.action == Action.EXIT:
                    log.info(f"[STRATEGY] exit ({decision.reason})")
                    await self.engine.handle_signal(Side.FLAT)
            except EngineBusyError as e:
                log.info(f"[STRATEGY] decision ignored, engine busy: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"[STRATEGY] decide/handle failed: {e}", exc_info=True)
            await asyncio.sleep(self.interval_sec)
