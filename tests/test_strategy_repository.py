import asyncio

from app.persistence.db import close_db, open_db
from app.persistence.strategy_repository import StrategyConfigRepository


def test_crud_round_trip():
    async def run():
        conn = await open_db(":memory:")
        try:
            repo = StrategyConfigRepository(conn)

            created = await repo.create("momentum", "My Momentum", {"lookback": 20})
            assert created["strategy_key"] == "momentum"
            assert created["name"] == "My Momentum"
            assert created["params"] == {"lookback": 20}
            assert created["enabled"] is False

            fetched = await repo.get(created["id"])
            assert fetched == created

            all_configs = await repo.list_all()
            assert len(all_configs) == 1
            assert all_configs[0]["id"] == created["id"]

            await repo.update_params(created["id"], "Renamed", {"lookback": 30})
            updated = await repo.get(created["id"])
            assert updated["name"] == "Renamed"
            assert updated["params"] == {"lookback": 30}
            assert updated["updated_at_ms"] >= created["updated_at_ms"]

            await repo.set_enabled(created["id"], True)
            enabled = await repo.get(created["id"])
            assert enabled["enabled"] is True

            await repo.delete(created["id"])
            assert await repo.get(created["id"]) is None
            assert await repo.list_all() == []
        finally:
            await close_db(conn)

    asyncio.run(run())


def test_list_all_multiple_configs_ordered_by_id():
    async def run():
        conn = await open_db(":memory:")
        try:
            repo = StrategyConfigRepository(conn)
            a = await repo.create("momentum", "A", {})
            b = await repo.create("mean_reversion", "B", {})
            configs = await repo.list_all()
            assert [c["id"] for c in configs] == [a["id"], b["id"]]
        finally:
            await close_db(conn)

    asyncio.run(run())
