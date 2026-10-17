from __future__ import annotations

import math
import time
from collections import deque
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition

DEFAULT_ACQUIRE_POLL = 0.5
DEFAULT_WARM_GRACE = 3.0

@dataclass
class _Slot:
    held: bool = False
    warm_owner: str | None = None
    reclaim_count: int = 0
    warm_until: float = math.inf
    released_at: float = -math.inf

class SlotSemaphore:
    """A counting semaphore whose slots remember their last owner.

    A plain semaphore wakes waiters in arbitrary order, so with many agents each
    model call may present a different conversation prefix, thrashing the
    server's KV prefix cache. Here a released slot stays reserved to its owner
    for ``warm_grace``, and the returning owner reclaims it ahead of the queue.
    Reservations lapse after ``warm_grace``, and an owner reclaims at most
    ``max_warm_run`` times in a row before the slot rotates out cold.
    """

    def __init__(
        self,
        n: int,
        max_warm_run: int = 32,
        warm_grace: float = DEFAULT_WARM_GRACE,
        poll: float = DEFAULT_ACQUIRE_POLL,
        clock: Callable[[], float] = time.monotonic,
        ):
        if n < 1:
            raise ValueError(f"SlotSemaphore needs at least 1 slot, got {n}")
        self._slots = [_Slot() for _ in range(n)]
        self._max_warm_run = max_warm_run
        self._warm_grace = warm_grace
        self._poll = poll
        self._clock = clock
        self._cond = Condition()
        self._queue: deque[str] = deque()

    @contextmanager
    def hold(self, owner: str, poll_abort: Callable[[], None] = lambda: None):
        """Acquire a slot for ``owner`` for the duration of the block. ``poll_abort``
        runs every poll cycle while waiting and may raise to abort the wait."""
        slot = self._acquire(owner, poll_abort)
        try:
            yield
        finally:
            self._release(slot)

    def _acquire(self, owner: str, poll_abort: Callable[[], None]) -> int:
        with self._cond:
            self._queue.append(owner)
            try:
                while True:
                    i, retry_at = self._take(owner)
                    if i is not None:
                        return i
                    self._cond.wait(min(max(retry_at - self._clock(), 0.01), self._poll))
                    poll_abort()
            finally:
                self._queue.remove(owner)
                self._cond.notify_all()  # leaving changes others' eviction order

    def _take(self, owner: str) -> tuple[int | None, float]:
        now = self._clock()
        head = bool(self._queue) and self._queue[0] == owner
        retry_at = math.inf
        cold_at, cold = math.inf, -1
        for i, slot in enumerate(self._slots):
            if slot.held:
                continue
            if slot.warm_owner == owner:
                self._claim(i, owner, warm=True)
                return i, 0.0
            if not head:
                continue
            if slot.warm_owner is None or slot.warm_until <= now:
                if slot.released_at < cold_at:  # LRU eviction
                    cold_at, cold = slot.released_at, i
                continue
            retry_at = min(retry_at, slot.warm_until)
        if cold != -1:
            self._claim(cold, owner, warm=False)
            return cold, 0.0
        return None, retry_at

    def _claim(self, i: int, owner: str, warm: bool) -> None:
        slot = self._slots[i]
        slot.held = True
        slot.warm_owner = owner
        slot.reclaim_count = slot.reclaim_count + 1 if warm else 1

    def _release(self, i: int) -> None:
        with self._cond:
            slot = self._slots[i]
            slot.held = False
            slot.released_at = self._clock()
            if slot.reclaim_count >= self._max_warm_run:
                slot.warm_owner = None  # cap reached: rotate the slot out cold
            else:
                slot.warm_until = self._clock() + self._warm_grace
            self._cond.notify_all()
