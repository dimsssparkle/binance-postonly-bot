from __future__ import annotations
from pathlib import Path

import aiosqlite

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def open_db(db_path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    schema = _SCHEMA_PATH.read_text()
    await conn.executescript(schema)
    await conn.commit()
    return conn


async def close_db(conn: aiosqlite.Connection) -> None:
    await conn.close()
