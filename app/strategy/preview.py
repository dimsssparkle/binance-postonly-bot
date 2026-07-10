"""Расчёт индикаторных рядов для визуального превью параметров стратегии на
графике дашборда (Фаза "TradingView-like превью") — те же чистые функции из
indicators.py, что использует реальный бэктест/live, посчитанные ТОЧКА-В-
ТОЧКУ (walk-forward, без заглядывания вперёд) на каждом баре, а не одно
значение на всё окно сразу — иначе линия на графике честно не отражала бы,
что видела бы стратегия в каждый конкретный момент истории.
"""
from __future__ import annotations
from typing import Sequence

from app.backtest.candle import Candle
from app.strategy.indicators import adx, rolling_high, rolling_low, rsi


def _price_channel_preview(candles: Sequence[Candle], lookback: int, limit: int) -> dict:
    start = max(0, len(candles) - limit)
    upper, lower = [], []
    for i in range(start, len(candles)):
        prior = candles[max(0, i - lookback):i]
        hi = rolling_high(prior, lookback)
        lo = rolling_low(prior, lookback)
        t = candles[i].open_time_ms // 1000
        if hi is not None:
            upper.append({"time": t, "value": hi})
        if lo is not None:
            lower.append({"time": t, "value": lo})
    return {"kind": "price_channel", "series": {"upper": upper, "lower": lower}}


def _rsi_preview(candles: Sequence[Candle], period: int, oversold: float, overbought: float,
                  limit: int) -> dict:
    start = max(0, len(candles) - limit)
    line = []
    for i in range(start, len(candles)):
        closes = [c.close for c in candles[:i + 1]]
        r = rsi(closes, period)
        if r is not None:
            line.append({"time": candles[i].open_time_ms // 1000, "value": r})
    return {"kind": "oscillator", "series": {"rsi": line},
            "thresholds": {"oversold": oversold, "overbought": overbought}}


def _adx_preview(candles: Sequence[Candle], adx_period: int, adx_threshold: float, limit: int) -> dict:
    start = max(0, len(candles) - limit)
    line = []
    for i in range(start, len(candles)):
        a = adx(candles[:i + 1], adx_period)
        if a is not None:
            line.append({"time": candles[i].open_time_ms // 1000, "value": a})
    return {"kind": "oscillator", "series": {"adx": line}, "thresholds": {"adx_threshold": adx_threshold}}


def compute_preview(strategy_key: str, params: dict, candles: Sequence[Candle], limit: int) -> dict:
    """candles — уже загруженный вызывающим срез истории (лимит показа +
    запас на разогрев индикатора ДО начала показа), в порядке oldest->newest.
    limit — сколько последних точек реально вернуть (запас используется
    только для корректного разогрева, сам по себе не возвращается)."""
    if strategy_key == "momentum":
        return _price_channel_preview(candles, int(params.get("lookback", 20)), limit)
    if strategy_key == "mean_reversion":
        return _rsi_preview(candles, int(params.get("period", 14)),
                             float(params.get("oversold", 30.0)), float(params.get("overbought", 70.0)), limit)
    if strategy_key == "regime_router":
        return _adx_preview(candles, int(params.get("adx_period", 14)),
                             float(params.get("adx_threshold", 25.0)), limit)
    raise ValueError(f"нет превью для стратегии: {strategy_key!r}")
