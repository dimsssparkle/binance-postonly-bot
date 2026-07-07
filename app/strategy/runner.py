from __future__ import annotations
import asyncio
import logging
from typing import Optional

from app.engine.exceptions import EngineBusyError
from app.engine.state_machine import ExecutionEngine
from app.strategy.base import Strategy

log = logging.getLogger("strategy_runner")


class StrategyRunner:
    """
    Периодически опрашивает Strategy.evaluate() и передаёт сигналы в
    ExecutionEngine.handle_signal — тем же путём, которым идёт ручной
    /trade/manual. Сам движок не отличает "сигнал от API" от "сигнал от
    стратегии", так что реальную стратегию можно будет подставить сюда
    без изменений в engine/ или api/.
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

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                signal = await self.strategy.evaluate()
                if signal is not None:
                    log.info(f"[STRATEGY] signal: {signal.side.value} {signal.symbol} (source={signal.source})")
                    await self.engine.handle_signal(signal.side)
            except EngineBusyError as e:
                log.info(f"[STRATEGY] signal ignored, engine busy: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"[STRATEGY] evaluate/handle failed: {e}", exc_info=True)
            await asyncio.sleep(self.interval_sec)
