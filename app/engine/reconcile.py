from __future__ import annotations
import logging

from app.engine.models import IntentState, OrderRole
from app.engine.state_machine import ExecutionEngine

log = logging.getLogger("reconcile")


class Reconciler:
    """
    При старте процесса сверяет персистентное состояние (intents/intent_orders)
    с реальностью на бирже вместо того, чтобы слепо доверять тому, что было
    записано перед крашем/рестартом.

    Два сценария:
    - Intent был в OPEN, но биржа уже flat (TP/SL сработали, пока процесс не
      работал) -> помечаем intent FLAT.
    - Intent был в OPEN, позиция всё ещё жива, но TP/SL не видно среди открытых
      algo-ордеров (крах случился между входом и подтверждением защиты) ->
      переставляем TP/SL заново.
    - Intent был в любом другом не-терминальном состоянии (NEW..PLACING_EXITS) —
      значит процесс упал посреди исполнения. Просто повторно запускаем
      _run_intent: каждый шаг там уже написан идемпотентно (проверяет текущую
      позицию/остаток перед действием), а client_order_id теперь строится на
      основе персистентного монотонного счётчика (intent.attempt_no), так что
      повторный запуск не пытается переиспользовать уже занятые ID ордеров.
    """

    def __init__(self, engine: ExecutionEngine) -> None:
        self.engine = engine

    async def run(self) -> None:
        e = self.engine
        active = await e.intents.list_active_all()
        if not active:
            log.info("[RECONCILE] no active intents to reconcile")
            return
        for intent in active:
            try:
                await self._reconcile_one(intent)
            except Exception as exc:
                log.error(f"[RECONCILE] intent #{intent.id} failed: {exc}", exc_info=True)

    async def _reconcile_one(self, intent) -> None:
        e = self.engine
        step = float(e.filters.get(intent.symbol)["stepSize"])
        pos_amt = e._get_position_amt(intent.symbol)
        is_flat = abs(pos_amt) <= step / 2

        if intent.state == IntentState.OPEN:
            if is_flat:
                await e.intents.update_state(intent.id, IntentState.FLAT)
                await e.events.append(
                    "engine", "reconciled_flat_on_startup",
                    {"reason": "position already flat on exchange"}, intent_id=intent.id,
                )
                log.info(f"[RECONCILE] intent #{intent.id}: was OPEN, exchange already flat -> FLAT")
                return

            await self._ensure_exits_alive(intent)
            return

        log.info(f"[RECONCILE] intent #{intent.id}: resuming from {intent.state.value}")
        await e.events.append(
            "engine", "reconciled_resume", {"from_state": intent.state.value}, intent_id=intent.id,
        )
        await e._run_intent(intent.id)

    async def _ensure_exits_alive(self, intent) -> None:
        e = self.engine
        try:
            algo_orders = e.rest.list_algo_open_orders(intent.symbol) or []
        except Exception as exc:
            log.warning(f"[RECONCILE] list_algo_open_orders failed for intent #{intent.id}: {exc}")
            return
        live_client_ids = {a.get("clientAlgoId") for a in algo_orders}

        order_rows = await e.orders.list_for_intent(intent.id)
        exit_rows = [o for o in order_rows if o.role in (OrderRole.TP, OrderRole.SL)]
        alive = [o for o in exit_rows if o.client_order_id in live_client_ids]

        if exit_rows and alive:
            log.info(f"[RECONCILE] intent #{intent.id}: exits still live on exchange, nothing to do")
            return

        log.warning(f"[RECONCILE] intent #{intent.id}: OPEN but no live exits found on exchange — replacing")
        try:
            e.rest.cancel_all_algo_orders(intent.symbol)
        except Exception:
            pass
        await e._place_exits(intent)
        await e.events.append("engine", "reconciled_exits_replaced", {}, intent_id=intent.id)
