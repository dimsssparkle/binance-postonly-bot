"""Загрузка и кэширование исторических klines для бэктеста.

Binance отдаёт максимум 1500 свечей за запрос, поэтому длинные периоды
тянем постранично (по closeTime вперёд). Кэшируем на диск в JSONL, чтобы
не перекачивать при каждом прогоне бэктеста.
"""
from __future__ import annotations
import json
import os
import time
from typing import Optional

from app.backtest.candle import Candle

_MS_PER_MIN = 60_000
_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
}


def _cache_path(cache_dir: str, symbol: str, interval: str) -> str:
    return os.path.join(cache_dir, f"{symbol.upper()}_{interval}.jsonl")


def fetch_klines(rest_client, symbol: str, interval: str,
                 start_ms: int, end_ms: Optional[int] = None,
                 max_per_req: int = 1500) -> list[Candle]:
    """Тянет закрытые свечи [start_ms, end_ms] постранично. end_ms=None -> сейчас.
    Возвращает Candle в порядке oldest->newest, без дублей, только закрытые."""
    if interval not in _TF_MINUTES:
        raise ValueError(f"unsupported interval: {interval}")
    end_ms = end_ms if end_ms is not None else int(time.time() * 1000)
    step = _TF_MINUTES[interval] * _MS_PER_MIN

    out: list[Candle] = []
    cursor = start_ms
    while cursor < end_ms:
        raw = rest_client.client.klines(
            symbol=symbol.upper(), interval=interval,
            startTime=cursor, endTime=end_ms, limit=max_per_req,
        )
        if not raw:
            break
        for k in raw:
            c = Candle.from_binance_kline(k)
            # только закрытые свечи (close_time в прошлом)
            if c.close_time_ms <= end_ms:
                out.append(c)
        last_open = int(raw[-1][0])
        nxt = last_open + step
        if nxt <= cursor:  # защита от зацикливания
            break
        cursor = nxt
        time.sleep(0.25)  # бережём rate limit

    # дедуп по open_time, сортировка
    seen = {}
    for c in out:
        seen[c.open_time_ms] = c
    return [seen[t] for t in sorted(seen)]


def save_candles(path: str, candles: list[Candle]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        for c in candles:
            f.write(json.dumps([
                c.open_time_ms, c.open, c.high, c.low, c.close, c.volume,
                c.close_time_ms, c.num_trades, c.taker_buy_base,
            ]) + "\n")


def load_candles(path: str) -> list[Candle]:
    out: list[Candle] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            a = json.loads(line)
            out.append(Candle(
                open_time_ms=int(a[0]), open=float(a[1]), high=float(a[2]),
                low=float(a[3]), close=float(a[4]), volume=float(a[5]),
                close_time_ms=int(a[6]), num_trades=int(a[7]), taker_buy_base=float(a[8]),
            ))
    return out


def get_history(rest_client, symbol: str, interval: str, days: int,
                cache_dir: str = "backtest_data", refresh: bool = False) -> list[Candle]:
    """Главный вход: вернуть ~`days` дней истории, из кэша или докачав."""
    path = _cache_path(cache_dir, symbol, interval)
    if os.path.exists(path) and not refresh:
        cached = load_candles(path)
        if cached:
            return cached
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * _MS_PER_MIN
    candles = fetch_klines(rest_client, symbol, interval, start_ms, end_ms)
    save_candles(path, candles)
    return candles
