"""Движок бэктеста: воспроизводит историю свеча-за-свечой и симулирует торговлю
той же Strategy, что и live. Модель исполнения намеренно ПЕССИМИСТИЧНА — если
стратегия прибыльна здесь, в реале (maker-вход дешевле) будет только лучше.

Пессимизм:
  - вход исполняется по OPEN СЛЕДУЮЩЕЙ 1m-свечи после закрытия 15m (нельзя
    действовать раньше, чем свеча закрылась), с taker-комиссией по умолчанию;
  - если внутри одной свечи задело и TP, и SL — считаем, что сработал SL;
  - выход всегда taker (TP/SL — market-type algo).

Одна позиция за раз (как в live): нашли вход -> ведём до выхода -> ищем следующий.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Sequence

from app.backtest.candle import Candle
from app.engine.fees import solve_exit_price_for_net_pnl
from app.engine.models import Side
from app.engine.rounding import round_to_step
from app.strategy.base import Action, PositionView, Strategy
from app.strategy.market_view import MarketView


@dataclass
class BacktestConfig:
    entry_tf: str = "15m"          # таймфрейм решения о входе
    exit_tf: str = "1m"            # таймфрейм ведения выхода
    maker_rate: float = 0.0002
    taker_rate: float = 0.0005
    entry_is_maker: bool = False   # False = пессимистично считаем вход как taker
    exit_mode: str = "fixed"       # "fixed" (TP+SL) | "dynamic" (hard SL + strategy EXIT)
    tp_pct: float = 0.0023         # цель чистой прибыли (доля нотионала)
    sl_pct: float = 0.0015         # допустимый чистый убыток
    qty: float = 1.0               # номинальный объём (метрики смотрим в долях, цена не важна)
    tick_size: str = "0.01"

    @property
    def entry_rate(self) -> float:
        return self.maker_rate if self.entry_is_maker else self.taker_rate


@dataclass
class Trade:
    side: Side
    entry_time_ms: int
    entry_price: float
    exit_time_ms: int
    exit_price: float
    qty: float
    entry_fee: float
    exit_fee: float
    gross_pnl: float
    net_pnl: float
    exit_reason: str
    bars_held: int
    entry_reason: str = ""

    @property
    def entry_notional(self) -> float:
        return self.entry_price * self.qty

    @property
    def net_return(self) -> float:
        """Чистый результат сделки как доля нотионала — ключевая метрика
        против пола комиссий 0.07%."""
        return self.net_pnl / self.entry_notional if self.entry_notional else 0.0


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    config: Optional[BacktestConfig] = None
    first_price: float = 0.0
    last_price: float = 0.0
    start_ms: int = 0
    end_ms: int = 0


def _fee_aware_levels(entry_price: float, side: Side, cfg: BacktestConfig):
    """(tp, sl) с учётом комиссий, округлённые к tick — как в live."""
    notional = entry_price * cfg.qty
    entry_fee = Decimal(str(notional * cfg.entry_rate))
    tp = None
    if cfg.exit_mode == "fixed" and cfg.tp_pct > 0:
        raw = solve_exit_price_for_net_pnl(entry_price, cfg.qty, entry_fee,
                                           cfg.taker_rate, notional * cfg.tp_pct, side)
        tp = float(round_to_step(raw, cfg.tick_size))
    sl = None
    if cfg.sl_pct > 0:
        raw = solve_exit_price_for_net_pnl(entry_price, cfg.qty, entry_fee,
                                           cfg.taker_rate, -(notional * cfg.sl_pct), side)
        sl = float(round_to_step(raw, cfg.tick_size))
    return tp, sl


def _check_hard(candle: Candle, side: Side, tp: Optional[float], sl: Optional[float]):
    """Задело ли TP/SL внутри свечи. При двойном срабатывании -> SL (пессимизм).
    Возвращает (hit, exit_price, reason) или (False, None, None)."""
    if side == Side.LONG:
        sl_hit = sl is not None and candle.low <= sl
        tp_hit = tp is not None and candle.high >= tp
    else:  # SHORT: SL выше входа, TP ниже
        sl_hit = sl is not None and candle.high >= sl
        tp_hit = tp is not None and candle.low <= tp
    if sl_hit and tp_hit:
        return True, sl, "sl(both_touched)"
    if sl_hit:
        return True, sl, "sl"
    if tp_hit:
        return True, tp, "tp"
    return False, None, None


def _make_trade(side: Side, entry_c_open: float, entry_time: int,
                exit_price: float, exit_time: int, reason: str, bars: int,
                cfg: BacktestConfig, entry_reason: str = "") -> Trade:
    qty = cfg.qty
    entry_fee = entry_c_open * qty * cfg.entry_rate
    exit_fee = exit_price * qty * cfg.taker_rate
    if side == Side.LONG:
        gross = (exit_price - entry_c_open) * qty
    else:
        gross = (entry_c_open - exit_price) * qty
    net = gross - entry_fee - exit_fee
    return Trade(side, entry_time, entry_c_open, exit_time, exit_price, qty,
                 entry_fee, exit_fee, gross, net, reason, bars, entry_reason)


class BacktestEngine:
    def __init__(self, cfg: BacktestConfig) -> None:
        self.cfg = cfg

    def run(self, strategy: Strategy,
            candles_by_tf: dict[str, Sequence[Candle]]) -> BacktestResult:
        cfg = self.cfg
        c_exit = candles_by_tf[cfg.exit_tf]
        c_entry = candles_by_tf[cfg.entry_tf]
        n = len(c_exit)
        result = BacktestResult(config=cfg)
        if n == 0 or not c_entry:
            return result

        # моменты закрытия entry-свечей (границы, на которых оцениваем вход)
        boundary = {c.close_time_ms for c in c_entry}

        result.first_price = c_exit[0].open
        result.last_price = c_exit[-1].close
        result.start_ms = c_exit[0].open_time_ms
        result.end_ms = c_exit[-1].close_time_ms

        j = 0
        while j < n - 1:
            c = c_exit[j]
            now = c.close_time_ms
            if now not in boundary:
                j += 1
                continue

            mv = MarketView(candles_by_tf, now)
            d = strategy.decide(mv, PositionView(Side.FLAT))
            if d.action not in (Action.ENTER_LONG, Action.ENTER_SHORT):
                j += 1
                continue

            side = Side.LONG if d.action == Action.ENTER_LONG else Side.SHORT
            entry_idx = j + 1  # вход по open следующей 1m-свечи
            entry_price = c_exit[entry_idx].open
            entry_time = c_exit[entry_idx].open_time_ms
            tp, sl = _fee_aware_levels(entry_price, side, cfg)

            # forward-scan до выхода
            k = entry_idx
            closed = False
            while k < n:
                ck = c_exit[k]
                hit, xprice, reason = _check_hard(ck, side, tp, sl)
                if hit:
                    result.trades.append(_make_trade(
                        side, entry_price, entry_time, xprice, ck.close_time_ms,
                        reason, k - entry_idx, cfg, entry_reason=d.reason))
                    closed = True
                    break
                if cfg.exit_mode == "dynamic":
                    mvk = MarketView(candles_by_tf, ck.close_time_ms)
                    posv = PositionView(side, entry_price, cfg.qty, k - entry_idx)
                    dk = strategy.decide(mvk, posv)
                    if dk.action == Action.EXIT and k + 1 < n:
                        xc = c_exit[k + 1]
                        result.trades.append(_make_trade(
                            side, entry_price, entry_time, xc.open, xc.close_time_ms,
                            "dynamic:" + (dk.reason or ""), k + 1 - entry_idx, cfg, entry_reason=d.reason))
                        closed = True
                        k += 1
                        break
                k += 1

            if not closed:
                # данные кончились с открытой позицией — mark-to-market по последней close
                last = c_exit[-1]
                result.trades.append(_make_trade(
                    side, entry_price, entry_time, last.close, last.close_time_ms,
                    "end_of_data", (n - 1) - entry_idx, cfg, entry_reason=d.reason))
                break

            j = k + 1

        return result
