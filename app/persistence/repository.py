from __future__ import annotations
import json
import time
from decimal import Decimal
from typing import Any, Optional

import aiosqlite

from app.engine.models import Intent, IntentOrder, IntentState, OrderRole, OrderStatus, Side


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row_to_intent(row: aiosqlite.Row) -> Intent:
    return Intent(
        id=row["id"],
        symbol=row["symbol"],
        desired_side=Side(row["desired_side"]),
        qty=row["qty"],
        state=IntentState(row["state"]),
        attempt_no=row["attempt_no"],
        entry_price=row["entry_price"],
        failure_reason=row["failure_reason"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _row_to_intent_order(row: aiosqlite.Row) -> IntentOrder:
    return IntentOrder(
        id=row["id"],
        intent_id=row["intent_id"],
        role=OrderRole(row["role"]),
        client_order_id=row["client_order_id"],
        exchange_order_id=row["exchange_order_id"],
        side=row["side"],
        order_type=row["order_type"],
        requested_qty=row["requested_qty"],
        requested_price=row["requested_price"],
        status=OrderStatus(row["status"]),
        filled_qty=row["filled_qty"],
        commission=row["commission"] if "commission" in row.keys() else "0",
        commission_asset=row["commission_asset"] if "commission_asset" in row.keys() else None,
        filled_price=row["filled_price"] if "filled_price" in row.keys() else None,
        realized_pnl=row["realized_pnl"] if "realized_pnl" in row.keys() else "0",
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


class IntentRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def create(self, symbol: str, desired_side: Side, qty: str) -> Intent:
        now = _now_ms()
        cur = await self._conn.execute(
            """INSERT INTO intents (symbol, desired_side, qty, state, attempt_no, created_at_ms, updated_at_ms)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (symbol.upper(), desired_side.value, qty, IntentState.NEW.value, now, now),
        )
        await self._conn.commit()
        return await self.get(cur.lastrowid)

    async def get(self, intent_id: int) -> Optional[Intent]:
        cur = await self._conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,))
        row = await cur.fetchone()
        return _row_to_intent(row) if row else None

    async def get_active(self, symbol: str) -> Optional[Intent]:
        cur = await self._conn.execute(
            "SELECT * FROM intents WHERE symbol = ? AND state NOT IN ('flat', 'failed') "
            "ORDER BY id DESC LIMIT 1",
            (symbol.upper(),),
        )
        row = await cur.fetchone()
        return _row_to_intent(row) if row else None

    async def get_previous_for_symbol(self, symbol: str, before_id: int) -> Optional[Intent]:
        """Intent, непосредственно предшествовавший этому по тому же символу —
        нужен, когда позиция закрылась НОВЫМ intent-ом (close_opposite), а не
        TP/SL внутри того же intent-а, что её открыл: комиссия входа осталась
        там, у предыдущего."""
        cur = await self._conn.execute(
            "SELECT * FROM intents WHERE symbol = ? AND id < ? ORDER BY id DESC LIMIT 1",
            (symbol.upper(), before_id),
        )
        row = await cur.fetchone()
        return _row_to_intent(row) if row else None

    async def list_active_all(self) -> list[Intent]:
        cur = await self._conn.execute(
            "SELECT * FROM intents WHERE state NOT IN ('flat', 'failed') ORDER BY id"
        )
        rows = await cur.fetchall()
        return [_row_to_intent(r) for r in rows]

    async def list_recent(self, limit: int = 50) -> list[Intent]:
        cur = await self._conn.execute(
            "SELECT * FROM intents ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [_row_to_intent(r) for r in rows]

    async def update_state(self, intent_id: int, state: IntentState, **fields: Any) -> None:
        sets = ["state = ?", "updated_at_ms = ?"]
        params: list[Any] = [state.value, _now_ms()]
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(value)
        params.append(intent_id)
        await self._conn.execute(f"UPDATE intents SET {', '.join(sets)} WHERE id = ?", params)
        await self._conn.commit()

    async def increment_attempt(self, intent_id: int) -> int:
        """Возвращает НОВОЕ значение attempt_no — используется как монотонный
        нонс для clientOrderId, чтобы повторный запуск intent-а после краха
        не пытался переиспользовать уже занятый client_order_id."""
        await self._conn.execute(
            "UPDATE intents SET attempt_no = attempt_no + 1, updated_at_ms = ? WHERE id = ?",
            (_now_ms(), intent_id),
        )
        await self._conn.commit()
        cur = await self._conn.execute("SELECT attempt_no FROM intents WHERE id = ?", (intent_id,))
        row = await cur.fetchone()
        return row["attempt_no"]


class IntentOrderRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def create(self, intent_id: int, role: OrderRole, client_order_id: str, side: str,
                      order_type: str, requested_qty: Optional[str] = None,
                      requested_price: Optional[str] = None) -> IntentOrder:
        now = _now_ms()
        await self._conn.execute(
            """INSERT INTO intent_orders
               (intent_id, role, client_order_id, side, order_type, requested_qty, requested_price,
                status, filled_qty, created_at_ms, updated_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '0', ?, ?)""",
            (intent_id, role.value, client_order_id, side, order_type, requested_qty,
             requested_price, OrderStatus.PENDING.value, now, now),
        )
        await self._conn.commit()
        return await self.get_by_client_order_id(client_order_id)

    async def get_by_client_order_id(self, client_order_id: str) -> Optional[IntentOrder]:
        cur = await self._conn.execute(
            "SELECT * FROM intent_orders WHERE client_order_id = ?", (client_order_id,)
        )
        row = await cur.fetchone()
        return _row_to_intent_order(row) if row else None

    async def list_for_intent(self, intent_id: int) -> list[IntentOrder]:
        cur = await self._conn.execute(
            "SELECT * FROM intent_orders WHERE intent_id = ? ORDER BY id", (intent_id,)
        )
        rows = await cur.fetchall()
        return [_row_to_intent_order(r) for r in rows]

    async def update_status(self, client_order_id: str, status: OrderStatus,
                             filled_qty: Optional[str] = None,
                             exchange_order_id: Optional[int] = None,
                             commission_delta: Optional[str] = None,
                             commission_asset: Optional[str] = None,
                             filled_price: Optional[str] = None,
                             realized_pnl_delta: Optional[str] = None) -> None:
        sets = ["status = ?", "updated_at_ms = ?"]
        params: list[Any] = [status.value, _now_ms()]
        if filled_qty is not None:
            sets.append("filled_qty = ?")
            params.append(filled_qty)
        if exchange_order_id is not None:
            sets.append("exchange_order_id = ?")
            params.append(exchange_order_id)
        if commission_delta is not None or realized_pnl_delta is not None:
            # WS шлёт комиссию/realizedPnl ЗА КАЖДЫЙ трейд (не кумулятивно),
            # поэтому накапливаем, а не перезаписываем — важно для частичных
            # исполнений.
            current = await self.get_by_client_order_id(client_order_id)
            if commission_delta is not None:
                prev = Decimal(current.commission or "0") if current else Decimal("0")
                sets.append("commission = ?")
                params.append(str(prev + Decimal(str(commission_delta))))
            if realized_pnl_delta is not None:
                prev_rp = Decimal(current.realized_pnl or "0") if current else Decimal("0")
                sets.append("realized_pnl = ?")
                params.append(str(prev_rp + Decimal(str(realized_pnl_delta))))
        if commission_asset is not None:
            sets.append("commission_asset = ?")
            params.append(commission_asset)
        if filled_price is not None:
            sets.append("filled_price = ?")
            params.append(filled_price)
        params.append(client_order_id)
        await self._conn.execute(
            f"UPDATE intent_orders SET {', '.join(sets)} WHERE client_order_id = ?", params
        )
        await self._conn.commit()

    async def sum_entry_commission(self, intent_id: int) -> Decimal:
        """Сумма фактических комиссий по entry-ордерам (maker+market fallback)
        этого intent-а — используется при расчёте net-TP/SL."""
        rows = await self.list_for_intent(intent_id)
        total = Decimal("0")
        for r in rows:
            if r.role in (OrderRole.ENTRY_MAKER, OrderRole.ENTRY_MARKET):
                total += Decimal(r.commission or "0")
        return total

    async def sum_all_commission(self, intent_id: int) -> Decimal:
        """Сумма комиссий по ВСЕМ ордерам intent-а (вход + выход)."""
        rows = await self.list_for_intent(intent_id)
        return sum((Decimal(r.commission or "0") for r in rows), Decimal("0"))

    async def get_closing_fill(self, intent_id: int) -> Optional[IntentOrder]:
        """Ордер, который фактически закрыл позицию (TP/SL сработал на бирже,
        либо ручное/встречное закрытие) — последний FILLED с такой ролью."""
        rows = await self.list_for_intent(intent_id)
        closing = [
            r for r in rows
            if r.role in (OrderRole.TP, OrderRole.SL, OrderRole.CLOSE_OPPOSITE)
            and r.status == OrderStatus.FILLED
        ]
        if not closing:
            return None
        return max(closing, key=lambda r: r.id or 0)


class EventLogRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def append(self, source: str, kind: str, payload: dict,
                      intent_id: Optional[int] = None) -> None:
        await self._conn.execute(
            "INSERT INTO events_log (ts_ms, source, kind, intent_id, payload_json) VALUES (?, ?, ?, ?, ?)",
            (_now_ms(), source, kind, intent_id, json.dumps(payload)),
        )
        await self._conn.commit()

    async def tail(self, limit: int = 100) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM events_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "ts_ms": r["ts_ms"],
                "source": r["source"],
                "kind": r["kind"],
                "intent_id": r["intent_id"],
                "payload": json.loads(r["payload_json"]),
            })
        return result


class ListenKeyRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def get(self) -> Optional[dict]:
        cur = await self._conn.execute("SELECT * FROM listen_key_state WHERE id = 1")
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "listen_key": row["listen_key"],
            "created_at_ms": row["created_at_ms"],
            "last_renewed_ms": row["last_renewed_ms"],
        }

    async def save(self, listen_key: str) -> None:
        now = _now_ms()
        await self._conn.execute(
            """INSERT INTO listen_key_state (id, listen_key, created_at_ms, last_renewed_ms)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET listen_key = excluded.listen_key,
                                              created_at_ms = excluded.created_at_ms,
                                              last_renewed_ms = excluded.last_renewed_ms""",
            (listen_key, now, now),
        )
        await self._conn.commit()


class SettingsRepository:
    """Персистентные runtime-настройки (переопределяют .env после первого
    сохранения через дашборд) — key/value в schema_meta."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def get(self, key: str) -> Optional[str]:
        cur = await self._conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set(self, key: str, value: str) -> None:
        await self._conn.execute(
            """INSERT INTO schema_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        await self._conn.commit()


class BookSnapshotRepository:
    """Запись компактных снимков стакана для будущих depth-стратегий."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, symbol: str, ts_ms: int, best_bid: str, best_bid_qty: str,
                     best_ask: str, best_ask_qty: str, bid_depth: str, ask_depth: str,
                     levels: int) -> None:
        await self._conn.execute(
            """INSERT INTO book_snapshots
               (ts_ms, symbol, best_bid, best_bid_qty, best_ask, best_ask_qty,
                bid_depth, ask_depth, levels)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts_ms, symbol.upper(), best_bid, best_bid_qty, best_ask, best_ask_qty,
             bid_depth, ask_depth, levels),
        )
        await self._conn.commit()

    async def count(self, symbol: Optional[str] = None) -> int:
        if symbol:
            cur = await self._conn.execute(
                "SELECT COUNT(*) AS n FROM book_snapshots WHERE symbol = ?", (symbol.upper(),))
        else:
            cur = await self._conn.execute("SELECT COUNT(*) AS n FROM book_snapshots")
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def count_since(self, symbol: str, since_ms: int) -> int:
        cur = await self._conn.execute(
            "SELECT COUNT(*) AS n FROM book_snapshots WHERE symbol = ? AND ts_ms >= ?",
            (symbol.upper(), since_ms),
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def latest_ts(self, symbol: str) -> Optional[int]:
        cur = await self._conn.execute(
            "SELECT MAX(ts_ms) AS ts FROM book_snapshots WHERE symbol = ?", (symbol.upper(),))
        row = await cur.fetchone()
        return int(row["ts"]) if row and row["ts"] is not None else None
