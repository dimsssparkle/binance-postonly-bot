"""Персистентность именованных конфигураций параметров стратегий,
настраиваемых с дашборда. Возвращает простые dict (не dataclass) — это
конфигурационные данные без собственного жизненного цикла состояния,
как ListenKeyRepository.get(), а не Intent/IntentOrder."""
from __future__ import annotations
import json
import time
from typing import Any, Optional

import aiosqlite


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row_to_config(row: aiosqlite.Row) -> dict:
    return {
        "id": row["id"],
        "strategy_key": row["strategy_key"],
        "name": row["name"],
        "params": json.loads(row["params_json"]),
        "enabled": bool(row["enabled"]),
        "created_at_ms": row["created_at_ms"],
        "updated_at_ms": row["updated_at_ms"],
    }


class StrategyConfigRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def create(self, strategy_key: str, name: str, params: dict[str, Any]) -> dict:
        now = _now_ms()
        cur = await self._conn.execute(
            """INSERT INTO strategy_configs
               (strategy_key, name, params_json, enabled, created_at_ms, updated_at_ms)
               VALUES (?, ?, ?, 0, ?, ?)""",
            (strategy_key, name, json.dumps(params), now, now),
        )
        await self._conn.commit()
        config = await self.get(cur.lastrowid)
        assert config is not None
        return config

    async def get(self, config_id: int) -> Optional[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM strategy_configs WHERE id = ?", (config_id,))
        row = await cur.fetchone()
        return _row_to_config(row) if row else None

    async def list_all(self) -> list[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM strategy_configs ORDER BY id")
        rows = await cur.fetchall()
        return [_row_to_config(r) for r in rows]

    async def update_params(self, config_id: int, name: str, params: dict[str, Any]) -> None:
        await self._conn.execute(
            """UPDATE strategy_configs SET name = ?, params_json = ?, updated_at_ms = ?
               WHERE id = ?""",
            (name, json.dumps(params), _now_ms(), config_id),
        )
        await self._conn.commit()

    async def set_enabled(self, config_id: int, enabled: bool) -> None:
        await self._conn.execute(
            "UPDATE strategy_configs SET enabled = ?, updated_at_ms = ? WHERE id = ?",
            (1 if enabled else 0, _now_ms(), config_id),
        )
        await self._conn.commit()

    async def delete(self, config_id: int) -> None:
        await self._conn.execute("DELETE FROM strategy_configs WHERE id = ?", (config_id,))
        await self._conn.commit()
