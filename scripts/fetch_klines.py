"""Скачать и закэшировать историю свечей для бэктеста.

Пример:
  python -m scripts.fetch_klines --symbol ETHUSDT --intervals 15m,1m --days 365
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.data import get_history
from app.exchange.rest import BinanceRestClient


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--intervals", default="15m,1m")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--cache-dir", default="backtest_data")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    client = BinanceRestClient()
    for interval in [s.strip() for s in args.intervals.split(",") if s.strip()]:
        print(f"fetching {args.symbol} {interval} ~{args.days}d ...", flush=True)
        candles = get_history(client, args.symbol, interval, args.days,
                              cache_dir=args.cache_dir, refresh=args.refresh)
        if candles:
            span_days = (candles[-1].close_time_ms - candles[0].open_time_ms) / 86_400_000
            print(f"  {len(candles)} candles, span {span_days:.1f}d "
                  f"[{candles[0].open_time_ms} .. {candles[-1].close_time_ms}]")
        else:
            print("  no candles returned")


if __name__ == "__main__":
    main()
