"""Запись стакана: слушает partial book depth по WebSocket и пишет компактные
снимки в SQLite ~раз в snapshot_interval секунд.

Цель — накопить свою историю L2 (глубины стакана), которой нет в бесплатных
klines, чтобы depth-стратегии стали бэктестируемыми через несколько недель.
Первой стратегией НЕ используется — это фоновый сбор данных.

Публичный market-data поток (без listenKey) через routed /public эндпоинт
(апгрейд Binance 2026-03-06). Формат сообщения проверен вживую:
поля 'b' (биды) и 'a' (аски) — массивы [price, qty], top-N уровней.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Optional

import websockets

from app.config import WS_BASE_URL
from app.persistence.repository import BookSnapshotRepository

log = logging.getLogger("market_stream")


class BookDepthRecorder:
    def __init__(self, snapshots: BookSnapshotRepository, symbol: str,
                 levels: int = 20, speed_ms: int = 100,
                 snapshot_interval_sec: float = 2.0) -> None:
        self.snapshots = snapshots
        self.symbol = symbol.upper()
        self.levels = levels
        self.speed_ms = speed_ms
        self.snapshot_interval_sec = snapshot_interval_sec
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self.connected = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever())
        log.info(f"BookDepthRecorder started ({self.symbol} depth{self.levels}@{self.speed_ms}ms, "
                 f"snapshot every {self.snapshot_interval_sec}s)")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()

    def _stream(self) -> str:
        return f"{self.symbol.lower()}@depth{self.levels}@{self.speed_ms}ms"

    async def _run_forever(self) -> None:
        backoff = 1.0
        last_persist = 0.0
        while not self._stopping:
            try:
                url = f"{WS_BASE_URL}/public/ws/{self._stream()}"
                async with websockets.connect(url, ping_interval=180, ping_timeout=600) as ws:
                    log.info("book depth stream connected")
                    self.connected = True
                    backoff = 1.0
                    async for raw in ws:
                        now = time.monotonic()
                        if now - last_persist < self.snapshot_interval_sec:
                            continue  # даунсэмплинг: не каждое 100ms-обновление
                        try:
                            await self._persist(raw)
                            last_persist = now
                        except Exception as e:
                            log.warning(f"[book] persist failed (ignored): {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                log.warning(f"book stream disconnected: {e!r}; reconnect in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _persist(self, raw: str) -> None:
        m = json.loads(raw)
        bids = m.get("b") or []
        asks = m.get("a") or []
        if not bids or not asks:
            return

        def _f(x) -> float:
            try:
                return float(x)
            except Exception:
                return 0.0

        best_bid, best_bid_qty = bids[0][0], bids[0][1]
        best_ask, best_ask_qty = asks[0][0], asks[0][1]
        bid_depth = sum(_f(b[1]) for b in bids[:self.levels])
        ask_depth = sum(_f(a[1]) for a in asks[:self.levels])
        ts_ms = int(m.get("E") or m.get("T") or int(time.time() * 1000))

        await self.snapshots.insert(
            symbol=self.symbol, ts_ms=ts_ms,
            best_bid=str(best_bid), best_bid_qty=str(best_bid_qty),
            best_ask=str(best_ask), best_ask_qty=str(best_ask_qty),
            bid_depth=f"{bid_depth:.8f}", ask_depth=f"{ask_depth:.8f}",
            levels=min(self.levels, len(bids), len(asks)),
        )
