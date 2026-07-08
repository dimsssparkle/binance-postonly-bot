from __future__ import annotations

from app.engine.models import Side


def compute_target_position(existing_amt: float, side: Side, qty: float, step: float) -> float:
    """
    Знаковый ЦЕЛЕВОЙ размер позиции (>0 LONG, <0 SHORT, 0 FLAT), если трактовать
    (side, qty) как СИГНАЛ-ДЕЛЬТУ поверх текущей позиции, а не абсолютный
    целевой размер. Вызывается РОВНО ОДИН РАЗ, при первом проходе intent-а,
    и персистится — НЕ пересчитывается заново при возобновлении после краха
    (иначе часть уже исполненной на бирже дельты потерялась бы: existing_amt
    на момент возобновления уже не тот, что был на момент прихода сигнала).

    side == FLAT всегда даёт цель 0 (полное закрытие), не подчиняется дельте.
    """
    if abs(existing_amt) <= step / 2:
        existing_amt = 0.0
    if side == Side.FLAT:
        return 0.0
    delta = qty if side == Side.LONG else -qty
    return existing_amt + delta


def compute_next_action(current_amt: float, target_amt: float, step: float) -> tuple[float, float]:
    """
    (close_qty, open_qty) — что нужно исполнить ПРЯМО СЕЙЧАС, чтобы приблизить
    текущую позицию к персистентной цели target_amt. В отличие от
    compute_target_position, это безопасно пересчитывать заново на КАЖДОМ
    проходе (включая возобновление после краха) — current_amt всегда живой,
    а target_amt зафиксирован, так что расхождение между ними ровно и есть
    то, что осталось сделать (никакого отдельного учёта "сколько уже закрыто
    в этом конкретном вызове" не требуется).
    """
    if abs(current_amt) <= step / 2:
        current_amt = 0.0
    if abs(target_amt) <= step / 2:
        target_amt = 0.0

    cur_sign = 0.0 if current_amt == 0.0 else (1.0 if current_amt > 0 else -1.0)
    tgt_sign = 0.0 if target_amt == 0.0 else (1.0 if target_amt > 0 else -1.0)

    if cur_sign == 0.0 or cur_sign == tgt_sign:
        # то же направление (или было плоско) — просто дотягиваем |current| до |target|
        if abs(target_amt) >= abs(current_amt):
            return 0.0, abs(target_amt) - abs(current_amt)  # добавить
        return abs(current_amt) - abs(target_amt), 0.0        # сократить, без переворота

    # разные знаки (включая target==0 при current!=0) — сперва закрыть всё
    # текущее, затем (если цель не 0) открыть цель с нуля
    return abs(current_amt), abs(target_amt)
