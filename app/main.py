from __future__ import annotations
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

from app.api.routes_control import router as control_router
from app.api.routes_dashboard import router as dashboard_router
from app.config import (
    CLOSE_TIMEOUT_MS, DB_PATH, HEDGE_MODE, LEVERAGE_DEFAULT, LOG_LEVEL,
    MAX_CLOSE_RETRIES, MAX_MAKER_ATTEMPTS, ORDER_TIMEOUT_MS, PORT, QTY_DEFAULT,
    SL_PCT, SYMBOL_DEFAULT, TP_PCT,
)
from app.engine.reconcile import Reconciler
from app.engine.state_machine import ExecutionEngine
from app.exchange.fees import CommissionRateCache
from app.exchange.filters import SymbolFilterCache
from app.exchange.leverage_brackets import LeverageBracketCache
from app.exchange.market_stream import BookDepthRecorder
from app.exchange.rest import BinanceRestClient
from app.exchange.ws_userstream import UserDataStream
from app.persistence.db import close_db, open_db
from app.persistence.repository import (
    BookSnapshotRepository, EventLogRepository, IntentOrderRepository, IntentRepository,
    ListenKeyRepository, SettingsRepository,
)
from app.strategy.noop import NoopStrategy
from app.strategy.runner import StrategyRunner

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def _ensure_symbol_setup(rest: BinanceRestClient, symbol: str, leverage: int) -> None:
    try:
        rest.set_position_mode(hedge=(HEDGE_MODE.lower() == "on"))
    except Exception as e:
        log.warning(f"Failed to set position mode: {e}")
    try:
        rest.set_margin_type_isolated(symbol)
    except Exception as e:
        log.warning(f"Failed to set margin type: {e}")
    try:
        rest.set_leverage(symbol, leverage)
    except Exception as e:
        log.warning(f"Failed to set leverage: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = await open_db(DB_PATH)
    rest = BinanceRestClient()
    filters = SymbolFilterCache(rest)
    commission_rates = CommissionRateCache(rest)
    leverage_brackets = LeverageBracketCache(rest)

    intents = IntentRepository(conn)
    orders = IntentOrderRepository(conn)
    events = EventLogRepository(conn)
    listen_keys = ListenKeyRepository(conn)
    settings = SettingsRepository(conn)
    book_snapshots = BookSnapshotRepository(conn)

    saved_leverage = await settings.get("leverage")
    saved_qty = await settings.get("qty_default")
    saved_tp = await settings.get("tp_pct")
    saved_sl = await settings.get("sl_pct")
    leverage = int(saved_leverage) if saved_leverage is not None else LEVERAGE_DEFAULT
    qty_default = saved_qty if saved_qty is not None else str(QTY_DEFAULT)
    tp_pct = float(saved_tp) if saved_tp is not None else TP_PCT
    sl_pct = float(saved_sl) if saved_sl is not None else SL_PCT

    _ensure_symbol_setup(rest, SYMBOL_DEFAULT, leverage)

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
        qty_default=qty_default,
        order_timeout_ms=ORDER_TIMEOUT_MS,
        close_timeout_ms=CLOSE_TIMEOUT_MS,
        max_maker_attempts=MAX_MAKER_ATTEMPTS,
        max_close_retries=MAX_CLOSE_RETRIES,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        leverage=leverage,
        commission_rates=commission_rates,
    )

    app.state.rest = rest
    app.state.engine = engine
    app.state.user_stream = user_stream
    app.state.settings = settings
    app.state.book_snapshots = book_snapshots
    app.state.leverage_brackets = leverage_brackets

    reconciler = Reconciler(engine)
    await reconciler.run()

    strategy = NoopStrategy()
    strategy_runner = StrategyRunner(strategy, engine)
    await strategy_runner.start()
    app.state.strategy_runner = strategy_runner

    # Фоновая запись стакана (Phase 2.P) — накапливает историю L2 для будущих
    # depth-стратегий. Не участвует в торговле.
    book_recorder = BookDepthRecorder(book_snapshots, SYMBOL_DEFAULT)
    await book_recorder.start()
    app.state.book_recorder = book_recorder

    log.info(f"Startup OK: engine ready for {SYMBOL_DEFAULT}")
    try:
        yield
    finally:
        await book_recorder.stop()
        await strategy_runner.stop()
        await user_stream.stop()
        await close_db(conn)


app = FastAPI(title="Binance Bot v2 (execution engine)", version="2.0.0", lifespan=lifespan)
app.include_router(control_router)
app.include_router(dashboard_router)
app.mount("/static", StaticFiles(directory="static", html=True), name="static")


@app.get("/")
def root_redirect():
    return RedirectResponse(url="/dashboard.html")


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=True)
