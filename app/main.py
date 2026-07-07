from __future__ import annotations
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.routes_control import router as control_router
from app.config import (
    CLOSE_TIMEOUT_MS, DB_PATH, HEDGE_MODE, LEVERAGE_DEFAULT, LOG_LEVEL,
    MAX_CLOSE_RETRIES, MAX_MAKER_ATTEMPTS, ORDER_TIMEOUT_MS, PORT, QTY_DEFAULT,
    SL_PCT, SYMBOL_DEFAULT, TP_PCT,
)
from app.engine.reconcile import Reconciler
from app.engine.state_machine import ExecutionEngine
from app.exchange.filters import SymbolFilterCache
from app.exchange.rest import BinanceRestClient
from app.exchange.ws_userstream import UserDataStream
from app.persistence.db import close_db, open_db
from app.persistence.repository import (
    EventLogRepository, IntentOrderRepository, IntentRepository, ListenKeyRepository,
)

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def _ensure_symbol_setup(rest: BinanceRestClient, symbol: str) -> None:
    try:
        rest.set_position_mode(hedge=(HEDGE_MODE.lower() == "on"))
    except Exception as e:
        log.warning(f"Failed to set position mode: {e}")
    try:
        rest.set_margin_type_isolated(symbol)
    except Exception as e:
        log.warning(f"Failed to set margin type: {e}")
    try:
        rest.set_leverage(symbol, LEVERAGE_DEFAULT)
    except Exception as e:
        log.warning(f"Failed to set leverage: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = await open_db(DB_PATH)
    rest = BinanceRestClient()
    filters = SymbolFilterCache(rest)

    _ensure_symbol_setup(rest, SYMBOL_DEFAULT)

    intents = IntentRepository(conn)
    orders = IntentOrderRepository(conn)
    events = EventLogRepository(conn)
    listen_keys = ListenKeyRepository(conn)

    user_stream = UserDataStream(
        rest=rest, listen_keys=listen_keys, orders=orders, intents=intents, events=events,
    )
    await user_stream.start()

    engine = ExecutionEngine(
        rest=rest,
        filters=filters,
        intents=intents,
        orders=orders,
        events=events,
        user_stream=user_stream,
        symbol=SYMBOL_DEFAULT,
        qty_default=str(QTY_DEFAULT),
        order_timeout_ms=ORDER_TIMEOUT_MS,
        close_timeout_ms=CLOSE_TIMEOUT_MS,
        max_maker_attempts=MAX_MAKER_ATTEMPTS,
        max_close_retries=MAX_CLOSE_RETRIES,
        tp_pct=TP_PCT,
        sl_pct=SL_PCT,
    )

    app.state.db = conn
    app.state.rest = rest
    app.state.engine = engine
    app.state.user_stream = user_stream

    reconciler = Reconciler(engine)
    await reconciler.run()

    log.info(f"Startup OK: engine ready for {SYMBOL_DEFAULT}")
    try:
        yield
    finally:
        await user_stream.stop()
        await close_db(conn)


app = FastAPI(title="Binance Bot v2 (execution engine)", version="2.0.0", lifespan=lifespan)
app.include_router(control_router)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=True)
