import pytest

from app.engine.netting import compute_target_position, compute_next_action
from app.engine.models import Side

STEP = 0.001


# --- compute_target_position: сигнал-дельта -> знаковая ЦЕЛЬ (считается один раз) ---

def test_target_flat_signal_is_zero():
    assert compute_target_position(-0.05, Side.FLAT, 0.0, STEP) == 0.0
    assert compute_target_position(0.05, Side.FLAT, 0.0, STEP) == 0.0
    assert compute_target_position(0.0, Side.FLAT, 0.0, STEP) == 0.0


def test_target_pure_add_from_flat():
    assert compute_target_position(0.0, Side.LONG, 0.02, STEP) == pytest.approx(0.02)
    assert compute_target_position(0.0, Side.SHORT, 0.02, STEP) == pytest.approx(-0.02)


def test_target_pure_add_same_direction():
    # SHORT 0.02 + сигнал SHORT 0.02 -> цель -0.04 (усреднение, не no-op)
    assert compute_target_position(-0.02, Side.SHORT, 0.02, STEP) == pytest.approx(-0.04)


def test_target_exact_close():
    # SHORT 0.02 + сигнал LONG 0.02 -> цель 0 (ровно закрыть)
    assert compute_target_position(-0.02, Side.LONG, 0.02, STEP) == pytest.approx(0.0)


def test_target_partial_reduce():
    # SHORT 0.02 + сигнал LONG 0.01 -> цель -0.01 (сократить, без переворота)
    assert compute_target_position(-0.02, Side.LONG, 0.01, STEP) == pytest.approx(-0.01)


def test_target_flip():
    # SHORT 0.02 + сигнал LONG 0.04 -> цель +0.02 (переворот)
    assert compute_target_position(-0.02, Side.LONG, 0.04, STEP) == pytest.approx(0.02)


def test_target_dust_existing_treated_as_flat():
    assert compute_target_position(0.0000001, Side.LONG, 0.02, STEP) == pytest.approx(0.02)


# --- compute_next_action: (close_qty, open_qty) от ЖИВОЙ позиции к цели —
# безопасно пересчитывать заново на каждом проходе, в т.ч. после краха ---

def test_action_pure_add_from_flat():
    assert compute_next_action(0.0, 0.02, STEP) == (0.0, pytest.approx(0.02))


def test_action_pure_add_same_direction():
    assert compute_next_action(-0.02, -0.04, STEP) == (0.0, pytest.approx(0.02))


def test_action_exact_close():
    assert compute_next_action(-0.02, 0.0, STEP) == (pytest.approx(0.02), 0.0)


def test_action_partial_reduce():
    assert compute_next_action(-0.02, -0.01, STEP) == (pytest.approx(0.01), 0.0)


def test_action_flip():
    assert compute_next_action(-0.02, 0.02, STEP) == (pytest.approx(0.02), pytest.approx(0.02))


def test_action_nothing_to_do_when_already_at_target():
    assert compute_next_action(0.02, 0.02, STEP) == (0.0, 0.0)
    assert compute_next_action(0.0, 0.0, STEP) == (0.0, 0.0)


def test_action_resumed_after_crash_mid_close_does_not_reopen():
    # Ровно закрыть (цель 0): крашнулись ПОСЛЕ того, как закрывающий ордер уже
    # исполнился на бирже -> при возобновлении current уже 0 -> ничего делать
    # не нужно, НЕ должно открыть лишнюю позицию (баг, найденный при трассировке).
    assert compute_next_action(0.0, 0.0, STEP) == (0.0, 0.0)


def test_action_resumed_after_crash_mid_flip_only_opens_remainder():
    # Флип: крашнулись ПОСЛЕ закрытия старой позиции, ДО открытия новой ->
    # при возобновлении current=0, цель=0.02 -> открыть ровно остаток 0.02,
    # без повторного/лишнего закрытия.
    assert compute_next_action(0.0, 0.02, STEP) == (0.0, pytest.approx(0.02))
