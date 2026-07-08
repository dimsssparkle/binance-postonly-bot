"""Живая цена последней сделки (тик) — для обновления текущей формирующейся
свечи на дашборде в реальном времени, так же как это делает сам Binance
(по цене последней сделки, а не по mid стакана).

Публичный поток <symbol>@trade через routed /public эндпоинт (тот же паттерн,
что и BookDepthRecorder). Проверено вживую: @aggTrade на этом роутинге НЕ
доставляет сообщений, а вот @trade — доставляет, формат подтверждён:
{"e":"trade","E":...,"T":...,"s":"ETHUSDT","t":...,"p":"1739.20","q":"9.927",...}
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional

import websockets

from app.config import WS_BASE_URL

log = logging.getLogger("trade_ticks")


class TradeTickRecorder:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self.connected = False
        self.last_price: Optional[float] = None
        self.last_trade_ms: Optional[int] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever())
        log.info(f"TradeTickRecorder started ({self.symbol}@trade)")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()

    def get_last_price(self) -> Optional[tuple[float, int]]:
        if not self.connected or self.last_price is None or self.last_trade_ms is None:
            return None
        return (self.last_price, self.last_trade_ms)

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                url = f"{WS_BASE_URL}/public/ws/{self.symbol.lower()}@trade"
                async with websockets.connect(url, ping_interval=180, ping_timeout=600) as ws:
                    log.info("trade tick stream connected")
                    self.connected = True
                    backoff = 1.0
                    async for raw in ws:
                        self._parse(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                log.warning(f"trade tick stream disconnected: {e!r}; reconnect in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _parse(self, raw: str) -> None:
        try:
            m = json.loads(raw)
            price = m.get("p")
            ts_ms = m.get("T") or m.get("E")
            if price is None or ts_ms is None:
                return
            self.last_price = float(price)
            self.last_trade_ms = int(ts_ms)
        except Exception:
            pass
