from collections import deque
from time import time
from typing import Deque, Tuple


class SignalRouter:
    """
    Отслеживает «шумность» сигналов и решает, нужно ли временно
    переключаться с post-only на market.
    """
    def __init__(self, W: int = 90, N: int = 4, F: int = 3, T_hold: int = 30, H: int = 60):
        """
        W      — окно (сек), в котором считаем частоты
        N      — сколько сигналов в окне считаем «много»
        F      — сколько переворотов направления (long<->short) считаем «много»
        T_hold — минимальное «время удержания» после входа (сек)
        H      — гистерезис (сек) — сколько держать режим spam после срабатывания
        """
        self.W, self.N, self.F, self.T_hold, self.H = W, N, F, T_hold, H
        self.events: Deque[Tuple[float, str]] = deque()  # (ts, side) side in {"long","short"}
        self.spam_until: float = 0.0
        self.last_open_ts: float = 0.0
        self.last_side: str | None = None

    def _purge(self, now: float) -> None:
        while self.events and now - self.events[0][0] > self.W:
            self.events.popleft()

    def register(self, side: str) -> None:
        now = time()
        self.events.append((now, side))
        self._purge(now)
        self.last_side = side

    def start_opened(self) -> None:
        self.last_open_ts = time()

    def in_spam(self) -> bool:
        """
        True — если стоит включить market-режим.
        Условия: много сигналов, много переворотов, или слишком ранний новый сигнал
        после недавнего входа (меньше T_hold).
        """
        now = time()
        self._purge(now)
        # если уже в spam — держим до истечения hysteresis
        if now < self.spam_until:
            return True

        count = len(self.events)
        flips = sum(
            1 for i in range(1, len(self.events))
            if self.events[i - 1][1] != self.events[i][1]
        )
        too_fast_reentry = (now - self.last_open_ts) < self.T_hold if self.last_open_ts else False

        spam = (count >= self.N) or (flips >= self.F) or too_fast_reentry
        if spam:
            self.spam_until = now + self.H
        return spam
