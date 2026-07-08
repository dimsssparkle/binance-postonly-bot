from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class OrderEvent:
    intent_id: int
    kind: str  # "entry" | "close"
    qty: Decimal  # filled_qty, > 0
    commission: Decimal
    realized_pnl: Decimal  # 0 для entry-событий


@dataclass(frozen=True)
class IntentAttribution:
    attributed_commission: Decimal  # собственная комиссия выхода этого intent-а
    # + его корректно взвешенная доля комиссии входа предков — это и есть
    # round-trip комиссия для этого intent-а
    realized_pnl: Decimal  # сумма по ВСЕМ close-ордерам этого intent-а (не
    # только по последнему — один intent может закрыть позицию несколькими
    # частичными филлами)
    has_close: bool


def allocate_lifecycle_commission(events: list[OrderEvent]) -> dict[int, IntentAttribution]:
    """events — FILLED entry/close ордера одного символа, от старых к новым,
    возможно охватывающие НЕСКОЛЬКО последовательных циклов flat->...->flat.

    Модель — взвешенное усреднение расходуемой базы (тот же принцип, что
    Binance использует для entryPrice: каждое добавление пересчитывает
    средневзвешенную цену/комиссию по остатку + новому объёму, частичное
    закрытие реализует PnL против этой средней, не меняя её для остатка).
    Поэтому расчёт комиссии остаётся согласован с уже доверенным realized_pnl,
    который Binance прислал по каждому филлу.

    Границы цикла (flat->flat) получаются АВТОМАТИЧЕСКИ, когда база объёма
    обнуляется — не выводятся из plan_target_amt (тот пишется ДО реального
    исполнения на бирже и может разойтись с фактом при FAILED intent-е с
    частичным реальным филлом). Поэтому это одинаково корректно работает и на
    цепочке частичных сокращений, и на перевороте (один intent одновременно
    закрывает старую позицию и открывает новую) — без специальной обработки:
    закрывающее событие обнуляет базу, следующее открывающее просто начинает
    копить её заново с нуля.
    """
    basis_commission = Decimal("0")
    basis_qty = Decimal("0")
    result: dict[int, IntentAttribution] = {}

    def bump(intent_id: int, *, attributed: Decimal = Decimal("0"),
             pnl: Decimal = Decimal("0"), closed: bool = False) -> None:
        prev = result.get(intent_id) or IntentAttribution(Decimal("0"), Decimal("0"), False)
        result[intent_id] = IntentAttribution(
            prev.attributed_commission + attributed,
            prev.realized_pnl + pnl,
            prev.has_close or closed,
        )

    for ev in events:
        if ev.kind == "entry":
            basis_commission += ev.commission
            basis_qty += ev.qty
            continue
        consumed = min(ev.qty, basis_qty) if basis_qty > 0 else Decimal("0")
        piece = (basis_commission * (consumed / basis_qty)) if basis_qty > 0 else Decimal("0")
        basis_commission = max(Decimal("0"), basis_commission - piece)
        basis_qty = max(Decimal("0"), basis_qty - consumed)
        bump(ev.intent_id, attributed=piece + ev.commission, pnl=ev.realized_pnl, closed=True)
    return result
