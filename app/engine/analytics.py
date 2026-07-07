from __future__ import annotations
import time
from decimal import Decimal
from typing import Optional

from app.engine.models import Intent, IntentOrder, OrderRole
from app.persistence.repository import IntentOrderRepository, IntentRepository


async def round_trip_commission(intents_repo: IntentRepository, orders_repo: IntentOrderRepository,
                                 intent: Intent, closing: IntentOrder) -> Decimal:
    """Суммарная комиссия по всей сделке. Комиссия за вход не всегда лежит
    в ЭТОМ intent-е: если позиция была закрыта не своим TP/SL, а НОВЫМ
    сигналом (close_opposite в другом intent-е), комиссия входа осталась
    в предыдущем intent-е того же символа — доучитываем её отдельно."""
    total = await orders_repo.sum_all_commission(intent.id)
    if closing.role == OrderRole.CLOSE_OPPOSITE:
        prior = await intents_repo.get_previous_for_symbol(intent.symbol, intent.id)
        if prior is not None:
            total += await orders_repo.sum_entry_commission(prior.id)
    return total


async def intent_net_realized_pnl(intents_repo: IntentRepository, orders_repo: IntentOrderRepository,
                                   intent: Intent) -> Optional[Decimal]:
    """Реализованный чистый PnL закрытого intent-а (realizedPnl биржи минус
    ВСЕ уплаченные комиссии, включая комиссию входа из предыдущего intent-а,
    если закрытие произошло через close_opposite). None, если позиция по
    этому intent-у ни разу не закрывалась реальным трейдом."""
    closing = await orders_repo.get_closing_fill(intent.id)
    if closing is None:
        return None
    total_commission = await round_trip_commission(intents_repo, orders_repo, intent, closing)
    return Decimal(closing.realized_pnl or "0") - total_commission


def start_of_day_ms(now_ms: Optional[int] = None) -> int:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return (now // 86_400_000) * 86_400_000


async def daily_net_pnl(intents_repo: IntentRepository, orders_repo: IntentOrderRepository,
                         symbol: str, limit: int = 500) -> Decimal:
    """Сумма чистого реализованного PnL по всем intent-ам этого символа,
    закрытым сегодня (граница дня — UTC, по времени сервера)."""
    since = start_of_day_ms()
    total = Decimal("0")
    rows = await intents_repo.list_recent(limit)
    for intent in rows:
        if intent.symbol.upper() != symbol.upper():
            continue
        if intent.updated_at_ms < since:
            continue
        if intent.state.value not in ("flat", "failed"):
            continue
        pnl = await intent_net_realized_pnl(intents_repo, orders_repo, intent)
        if pnl is not None:
            total += pnl
    return total
