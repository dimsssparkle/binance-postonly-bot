from __future__ import annotations
from pathlib import Path

import aiosqlite

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Лёгкие миграции для колонок, добавленных после первого релиза схемы —
# CREATE TABLE IF NOT EXISTS не трогает уже существующие таблицы, поэтому
# новые столбцы на старой БД нужно добавлять отдельно (idempotent: ловим
# "duplicate column" и просто пропускаем).
_COLUMN_MIGRATIONS = [
    ("intent_orders", "commission", "TEXT NOT NULL DEFAULT '0'"),
    ("intent_orders", "commission_asset", "TEXT"),
    ("intent_orders", "filled_price", "TEXT"),
    ("intent_orders", "realized_pnl", "TEXT NOT NULL DEFAULT '0'"),
    ("intents", "plan_target_amt", "TEXT"),
]


async def _run_column_migrations(conn: aiosqlite.Connection) -> None:
    for table, column, ddl in _COLUMN_MIGRATIONS:
        try:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
    await conn.commit()


async def open_db(db_path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    schema = _SCHEMA_PATH.read_text()
    await conn.executescript(schema)
    await conn.commit()
    await _run_column_migrations(conn)
    return conn


async def close_db(conn: aiosqlite.Connection) -> None:
    await conn.close()
