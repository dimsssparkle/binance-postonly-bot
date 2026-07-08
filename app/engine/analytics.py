from __future__ import annotations
import time
from decimal import Decimal
from typing import Optional

from app.engine.commission_ledger import IntentAttribution, OrderEvent, allocate_lifecycle_commission
from app.engine.models import Intent, OrderRole
from app.persistence.repository import IntentOrderRepository, IntentRepository


async def intent_net_realized_pnl_batch(orders_repo: IntentOrderRepository, symbol: str,
                                         up_to_intent_id: int) -> dict[int, IntentAttribution]:
    """Честное распределение комиссии/PnL по всем intent-ам символа (вплоть
    до up_to_intent_id), что закрывали позицию частично или полностью —
    один проход по истории вместо запроса на каждый intent (см.
    commission_ledger.allocate_lifecycle_commission)."""
    rows = await orders_repo.list_filled_for_symbol(symbol, up_to_intent_id)
    events = [
        OrderEvent(
            intent_id=r.intent_id,
            kind="entry" if r.role in (OrderRole.ENTRY_MAKER, OrderRole.ENTRY_MARKET) else "close",
            qty=Decimal(r.filled_qty or "0"),
            commission=Decimal(r.commission or "0"),
            realized_pnl=Decimal(r.realized_pnl or "0"),
        )
        for r in rows
    ]
    return allocate_lifecycle_commission(events)


async def intent_net_realized_pnl(orders_repo: IntentOrderRepository, intent: Intent) -> Optional[Decimal]:
    """Реализованный чистый PnL ЭТОГО intent-а (сумма realizedPnl биржи по
    всем его closing-филлам минус честно распределённая round-trip комиссия,
    включая долю входа из intent-ов-добавлений, что были раньше в цепочке).
    None, если этот intent ни разу не закрывал позицию реальным трейдом.

    Для расчёта по многим intent-ам подряд (дашборд, дневной PnL) вызывайте
    intent_net_realized_pnl_batch один раз и берите значения из словаря —
    эта функция сама под капотом делает ровно такой батч-запрос, так что
    вызывать её в цикле по нескольким intent-ам того же символа неэффективно."""
    attrs = await intent_net_realized_pnl_batch(orders_repo, intent.symbol, intent.id)
    a = attrs.get(intent.id)
    if a is None or not a.has_close:
        return None
    return a.realized_pnl - a.attributed_commission


def start_of_day_ms(now_ms: Optional[int] = None) -> int:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return (now // 86_400_000) * 86_400_000


async def daily_net_pnl(intents_repo: IntentRepository, orders_repo: IntentOrderRepository,
                         symbol: str, limit: int = 500) -> Decimal:
    """Сумма чистого реализованного PnL по всем intent-ам этого символа,
    обновлённым сегодня (граница дня — UTC, по времени сервера). Не
    фильтруем по state == flat/failed — у intent-а-переворота (state==open,
    новая сторона всё ещё открыта) уже реализован реальный PnL по закрытой
    старой стороне, и он должен попасть в дневную сумму; фильтр по
    has_close (через intent_net_realized_pnl_batch) корректно это учитывает."""
    since = start_of_day_ms()
    rows = await intents_repo.list_recent(limit)
    todays = [i for i in rows if i.symbol.upper() == symbol.upper() and i.updated_at_ms >= since]
    if not todays:
        return Decimal("0")
    attrs = await intent_net_realized_pnl_batch(orders_repo, symbol, max(i.id for i in todays))
    total = Decimal("0")
    for intent in todays:
        a = attrs.get(intent.id)
        if a is not None and a.has_close:
            total += a.realized_pnl - a.attributed_commission
    return total
