"""Запись стакана: слушает partial book depth по WebSocket и пишет компактные
снимки в SQLite ~раз в snapshot_interval секунд.

Цель — накопить свою историю L2 (глубины стакана), которой нет в бесплатных
klines, чтобы depth-стратегии стали бэктестируемыми через несколько недель.

Также кэширует лучшую цену (best bid/ask) в памяти на КАЖДОЕ сообщение
(~100ms) — используется движком для ценообразования ордеров вместо REST
book_ticker (см. ExecutionEngine._get_book), с фолбэком на REST если поток
не подключён/данные протухли (BOOK_CACHE_MAX_STALENESS_MS).

Публичный market-data поток (без listenKey) через routed /public эндпоинт
(апгрейд Binance 2026-03-06). Формат сообщения проверен вживую:
поля 'b' (биды) и 'a' (аски) — массивы [price, qty], top-N уровней.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Any, Optional

import websockets

from app.config import BOOK_CACHE_MAX_STALENESS_MS, WS_BASE_URL
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

        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None
        self._last_update_monotonic: Optional[float] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever())
        log.info(f"BookDepthRecorder started ({self.symbol} depth{self.levels}@{self.speed_ms}ms, "
                 f"snapshot every {self.snapshot_interval_sec}s)")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()

    def get_best_prices(self) -> Optional[tuple[float, float]]:
        """(bid, ask) из живого WS-потока, свежее BOOK_CACHE_MAX_STALENESS_MS.
        None, если поток не подключён/данных ещё не было/протухли — сигнал
        вызывающему коду откатиться на REST book_ticker."""
        if not self.connected or self.best_bid is None or self.best_ask is None:
            return None
        if self._last_update_monotonic is None:
            return None
        age_ms = (time.monotonic() - self._last_update_monotonic) * 1000
        if age_ms > BOOK_CACHE_MAX_STALENESS_MS:
            return None
        return (self.best_bid, self.best_ask)

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
                        parsed = self._parse(raw)
                        if parsed is not None:
                            self.best_bid = parsed["best_bid"]
                            self.best_ask = parsed["best_ask"]
                            self._last_update_monotonic = now
                        if now - last_persist < self.snapshot_interval_sec:
                            continue  # даунсэмплинг: не каждое 100ms-обновление пишем в БД
                        if parsed is None:
                            continue
                        try:
                            await self._persist(parsed)
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

    def _parse(self, raw: str) -> Optional[dict[str, Any]]:
        """Разбор сообщения — вызывается на КАЖДОЕ сообщение (~100ms), должен
        быть дешёвым и никогда не бросать исключение наружу (иначе уронит
        соединение по вине одного битого кадра)."""
        try:
            m = json.loads(raw)
            bids = m.get("b") or []
            asks = m.get("a") or []
            if not bids or not asks:
                return None

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

            return {
                "best_bid": float(best_bid), "best_bid_qty": best_bid_qty,
                "best_ask": float(best_ask), "best_ask_qty": best_ask_qty,
                "bid_depth": bid_depth, "ask_depth": ask_depth, "ts_ms": ts_ms,
                "levels": min(self.levels, len(bids), len(asks)),
            }
        except Exception:
            return None

    async def _persist(self, parsed: dict[str, Any]) -> None:
        await self.snapshots.insert(
            symbol=self.symbol, ts_ms=parsed["ts_ms"],
            best_bid=str(parsed["best_bid"]), best_bid_qty=str(parsed["best_bid_qty"]),
            best_ask=str(parsed["best_ask"]), best_ask_qty=str(parsed["best_ask_qty"]),
            bid_depth=f"{parsed['bid_depth']:.8f}", ask_depth=f"{parsed['ask_depth']:.8f}",
            levels=parsed["levels"],
        )
