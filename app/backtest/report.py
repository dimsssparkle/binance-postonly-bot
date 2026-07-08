"""Метрики бэктеста. Главная — средний чистый результат на сделку против пола
комиссий: стратегия, чей средний edge не перекрывает round-trip комиссию,
убыточна по построению."""
from __future__ import annotations
from dataclasses import dataclass

from app.backtest.engine import BacktestResult


@dataclass
class Report:
    num_trades: int
    num_wins: int
    win_rate: float
    total_net_pnl: float          # в котируемой валюте (USDT при qty из конфига)
    total_gross_pnl: float
    total_fees: float
    avg_net_return: float         # средняя доля нотионала на сделку — ключевая
    fee_floor: float              # round-trip пол комиссий (для сравнения)
    max_drawdown: float           # по кривой чистого PnL, в котируемой валюте
    profit_factor: float
    avg_bars_held: float
    buy_hold_return: float        # доходность buy&hold за тот же период (доля)
    strategy_return: float        # суммарная доля (сумма net_return по сделкам)

    def format(self) -> str:
        floor_verdict = ("ВЫШЕ пола ✓" if self.avg_net_return > self.fee_floor
                         else "НИЖЕ пола ✗ — убыточно по построению")
        lines = [
            f"  Сделок:                 {self.num_trades}",
            f"  Винрейт:                {self.win_rate*100:.1f}%  ({self.num_wins}/{self.num_trades})",
            f"  Чистый PnL (сумма):     {self.total_net_pnl:+.4f}",
            f"  Валовый PnL:            {self.total_gross_pnl:+.4f}",
            f"  Комиссии (всего):       {self.total_fees:.4f}",
            f"  Ср. чистый на сделку:   {self.avg_net_return*100:+.4f}%   "
            f"(пол комиссий {self.fee_floor*100:.4f}%) -> {floor_verdict}",
            f"  Макс. просадка:         {self.max_drawdown:.4f}",
            f"  Profit factor:          {self.profit_factor:.2f}",
            f"  Ср. удержание (баров):  {self.avg_bars_held:.1f}",
            f"  Стратегия (сумма долей):{self.strategy_return*100:+.2f}%",
            f"  Buy & hold за период:   {self.buy_hold_return*100:+.2f}%",
        ]
        return "\n".join(lines)


def build_report(result: BacktestResult) -> Report:
    trades = result.trades
    cfg = result.config
    fee_floor = (cfg.entry_rate + cfg.taker_rate) if cfg else 0.0007

    n = len(trades)
    if n == 0:
        return Report(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, fee_floor, 0.0, 0.0, 0.0,
                      _buy_hold(result), 0.0)

    wins = [t for t in trades if t.net_pnl > 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    total_net = sum(t.net_pnl for t in trades)
    total_gross = sum(t.gross_pnl for t in trades)
    total_fees = sum(t.entry_fee + t.exit_fee for t in trades)

    # просадка по кумулятивной кривой чистого PnL
    peak, dd, cum = 0.0, 0.0, 0.0
    for t in trades:
        cum += t.net_pnl
        peak = max(peak, cum)
        dd = max(dd, peak - cum)

    return Report(
        num_trades=n,
        num_wins=len(wins),
        win_rate=len(wins) / n,
        total_net_pnl=total_net,
        total_gross_pnl=total_gross,
        total_fees=total_fees,
        avg_net_return=sum(t.net_return for t in trades) / n,
        fee_floor=fee_floor,
        max_drawdown=dd,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        avg_bars_held=sum(t.bars_held for t in trades) / n,
        buy_hold_return=_buy_hold(result),
        strategy_return=sum(t.net_return for t in trades),
    )


def _buy_hold(result: BacktestResult) -> float:
    if result.first_price <= 0:
        return 0.0
    return (result.last_price - result.first_price) / result.first_price
