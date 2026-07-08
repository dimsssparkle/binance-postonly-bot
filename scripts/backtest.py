"""Прогнать стратегию по закэшированной истории и напечатать отчёт.

Примеры:
  python -m scripts.backtest --strategy always_long --tp 0 --sl 0
  python -m scripts.backtest --strategy random
  python -m scripts.backtest --strategy momentum --exit fixed
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.data import load_candles, _cache_path
from app.backtest.engine import BacktestConfig, BacktestEngine
from app.backtest.report import build_report


def _make_strategy(name: str, args):
    if name == "always_long":
        from app.strategy.sanity import AlwaysLongStrategy
        return AlwaysLongStrategy()
    if name == "random":
        from app.strategy.sanity import RandomStrategy
        return RandomStrategy(seed=args.seed)
    if name == "momentum":
        from app.strategy.momentum import MomentumStrategy
        return MomentumStrategy()
    if name == "mean_reversion":
        from app.strategy.mean_reversion import MeanReversionStrategy
        return MeanReversionStrategy()
    raise SystemExit(f"unknown strategy: {name}")


def _split(candles, frac):
    if frac <= 0:
        return candles, []
    cut = int(len(candles) * (1 - frac))
    return candles[:cut], candles[cut:]


def _run_and_print(label, strategy, candles_by_tf, cfg):
    result = BacktestEngine(cfg).run(strategy, candles_by_tf)
    rep = build_report(result)
    print(f"\n=== {label} ({cfg.exit_mode}, tp={cfg.tp_pct} sl={cfg.sl_pct}) ===")
    print(rep.format())
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--symbol", default="ETHUSDT")
    ap.add_argument("--entry-tf", default="15m")
    ap.add_argument("--exit-tf", default="1m")
    ap.add_argument("--exit", dest="exit_mode", default="fixed", choices=["fixed", "dynamic"])
    ap.add_argument("--tp", type=float, default=0.0023)
    ap.add_argument("--sl", type=float, default=0.0015)
    ap.add_argument("--maker-entry", action="store_true", help="считать вход maker (оптимистично)")
    ap.add_argument("--oos", type=float, default=0.3, help="доля out-of-sample хвоста")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cache-dir", default="backtest_data")
    args = ap.parse_args()

    c_entry = load_candles(_cache_path(args.cache_dir, args.symbol, args.entry_tf))
    c_exit = load_candles(_cache_path(args.cache_dir, args.symbol, args.exit_tf))
    print(f"loaded {args.entry_tf}={len(c_entry)}  {args.exit_tf}={len(c_exit)} candles")

    cfg = BacktestConfig(
        entry_tf=args.entry_tf, exit_tf=args.exit_tf, exit_mode=args.exit_mode,
        tp_pct=args.tp, sl_pct=args.sl, entry_is_maker=args.maker_entry,
    )

    # in-sample / out-of-sample по времени (хвост = OOS)
    entry_is, entry_oos = _split(c_entry, args.oos)
    exit_is, exit_oos = _split(c_exit, args.oos)

    _run_and_print("IN-SAMPLE", _make_strategy(args.strategy, args),
                   {args.entry_tf: entry_is, args.exit_tf: exit_is}, cfg)
    if args.oos > 0:
        _run_and_print("OUT-OF-SAMPLE", _make_strategy(args.strategy, args),
                       {args.entry_tf: entry_oos, args.exit_tf: exit_oos}, cfg)
    _run_and_print("FULL", _make_strategy(args.strategy, args),
                   {args.entry_tf: c_entry, args.exit_tf: c_exit}, cfg)


if __name__ == "__main__":
    main()
