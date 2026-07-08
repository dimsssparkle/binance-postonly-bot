"""Разовая проверка Milestone 1: REST-клиент, округление, SQLite-персистентность.

Запуск: python -m scripts.verify_foundations
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.exchange.rest import BinanceRestClient
from app.exchange.filters import SymbolFilterCache
from app.engine.rounding import round_to_step, round_up_to_step
from app.engine.models import Side
from app.persistence.db import open_db, close_db
from app.persistence.repository import IntentRepository
from app.config import SYMBOL_DEFAULT


async def main() -> None:
    print("== REST: exchange_info + filters ==")
    client = BinanceRestClient()
    cache = SymbolFilterCache(client)
    filters = cache.get(SYMBOL_DEFAULT)
    print(f"{SYMBOL_DEFAULT} filters: {filters}")

    print("== rounding ==")
    price = 3123.4567
    qty = 0.123456
    print(f"round_to_step(price, tick) = {round_to_step(price, filters['tickSize'])}")
    print(f"round_to_step(qty, step)   = {round_to_step(qty, filters['stepSize'])}")
    print(f"round_up_to_step(qty, step) = {round_up_to_step(qty, filters['stepSize'])}")

    print("== SQLite: insert/read Intent ==")
    test_db_path = "verify_foundations_tmp.db"
    if os.path.exists(test_db_path):
        os.remove(test_db_path)
    conn = await open_db(test_db_path)
    try:
        repo = IntentRepository(conn)
        intent = await repo.create(SYMBOL_DEFAULT, Side.LONG, "0.01")
        print(f"created intent: {intent}")
        fetched = await repo.get(intent.id)
        print(f"fetched intent: {fetched}")
        active = await repo.get_active(SYMBOL_DEFAULT)
        print(f"active intent for {SYMBOL_DEFAULT}: {active}")
        assert fetched is not None and fetched.id == intent.id
        assert active is not None and active.id == intent.id
        print("OK: intent persisted and readable")
    finally:
        await close_db(conn)
        os.remove(test_db_path)

    print("\nALL MILESTONE 1 CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
