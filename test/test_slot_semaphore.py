import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from xun.slot_semaphore import SlotSemaphore, _Slot

POLL = 0.01
GRACE = 0.15


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("condition not met in time")


class _Counter:
    def __init__(self):
        self.lock = Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def hold(self, gate: SlotSemaphore, owner: str, secs: float = 0.01):
        with gate.hold(owner):
            with self.lock:
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            time.sleep(secs)
            with self.lock:
                self.in_flight -= 1


class MutualExclusionTest(unittest.TestCase):
    def test_concurrency_capped(self):
        gate = SlotSemaphore(3, warm_grace=GRACE, poll=POLL)
        counter = _Counter()
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda i: counter.hold(gate, f"agent-{i}"), range(24)))
        self.assertLessEqual(counter.max_in_flight, 3)

    def test_invalid_n(self):
        with self.assertRaises(ValueError):
            SlotSemaphore(0)

    def test_cancel_aborts_wait(self):
        gate = SlotSemaphore(1, warm_grace=GRACE, poll=POLL)
        with gate.hold("blocker"):
            calls = []

            def cancel():
                calls.append(1)
                if len(calls) >= 3:
                    raise TimeoutError("cancel fired")

            with self.assertRaises(TimeoutError):
                with gate.hold("waiter", cancel):
                    pass  # unreachable

        # the aborted wait left no residue: the slot is still usable
        with gate.hold("late"):
            pass

    def test_release_after_exception_frees_slot(self):
        gate = SlotSemaphore(1, warm_grace=GRACE, poll=POLL)
        with self.assertRaises(RuntimeError):
            with gate.hold("A"):
                raise RuntimeError("boom")
        # release ran in the finally: B proceeds (after at most one grace window)
        with gate.hold("B"):
            pass

    def test_grace_bounded_wait(self):
        # the head must progress within about one grace period
        gate = SlotSemaphore(1, max_warm_run=100, warm_grace=GRACE, poll=POLL)
        with gate.hold("A"):
            pass
        start = time.monotonic()
        with gate.hold("B"):
            pass
        self.assertLess(time.monotonic() - start, 2.0 * GRACE + 0.5)


class AffinityTest(unittest.TestCase):
    def test_owner_reclaims_warm_slot_ahead_of_queue(self):
        gate = SlotSemaphore(1, max_warm_run=3, warm_grace=GRACE, poll=POLL)
        with gate.hold("A"):
            pass  # slot reserved for A

        order = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut_b = pool.submit(self._hold_marked, gate, order, "B")
            _wait_until(lambda: "B" in gate._queue)
            with gate.hold("A"):
                order.append("A")  # reclaims inside the grace window
            fut_b.result(10)
        # without affinity the queued head B would have gone first
        self.assertEqual(order, ["A", "B"])

    def test_head_evicts_after_grace_lapses(self):
        gate = SlotSemaphore(1, max_warm_run=100, warm_grace=GRACE, poll=POLL)
        with gate.hold("A"):
            pass
        order = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut_b = pool.submit(self._hold_marked, gate, order, "B")
            _wait_until(lambda: "B" in gate._queue)
            time.sleep(2 * GRACE)  # A stays away, the reservation lapses
            fut_b.result(10)  # B takes the slot despite A's hint
        self.assertEqual(order, ["B"])

    def test_warm_run_capped_forces_rotation(self):
        gate = SlotSemaphore(1, max_warm_run=2, warm_grace=GRACE, poll=POLL)
        takes: list[str] = []

        def loop(owner: str, n: int):
            for _ in range(n):
                with gate.hold(owner):
                    takes.append(owner)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(loop, "A", 6)
            fut_b = pool.submit(loop, "B", 6)
            fut_a.result(10)
            fut_b.result(10)
        # fairness: a rival looping forever still completes every take
        self.assertEqual(takes.count("B"), 6, "B must not starve")

    def test_cap_rotation_lets_waiting_head_in_without_grace(self):
        # at the cap the slot goes cold: a waiting head enters despite 100s grace
        now = [100.0]
        gate = SlotSemaphore(1, max_warm_run=2, warm_grace=100.0, poll=POLL, clock=lambda: now[0])

        self.assertEqual(self._head_take(gate, "A"), 0)  # run 1
        gate._release(0)  # below cap: released warm
        gate._queue.append("B")  # B parks waiting
        self.assertEqual(self._head_take(gate, "A"), 0)  # warm reclaim, run == cap
        gate._release(0)
        with gate._cond:
            self.assertIsNone(gate._slots[0].warm_owner)  # released cold
            self.assertEqual(gate._take("B")[0], 0)  # head in immediately, no grace wait

    def test_release_clears_warm_at_cap(self):
        gate = SlotSemaphore(1, max_warm_run=2, warm_grace=GRACE, poll=POLL)
        with gate._cond:
            gate._slots[0] = _Slot(reclaim_count=2, warm_owner="A")  # already at the cap
            gate._release(0)
            self.assertIsNone(gate._slots[0].warm_owner)
        with gate._cond:
            gate._slots[0] = _Slot(reclaim_count=1, warm_owner="A")  # below the cap
            gate._release(0)
            self.assertEqual(gate._slots[0].warm_owner, "A")

    def test_cold_slot_preferred_over_reserved(self):
        gate = SlotSemaphore(3, max_warm_run=100, warm_grace=GRACE, poll=POLL)
        with gate.hold("A"):
            pass  # one slot reserved for A
        with gate.hold("C"):
            reserved = [s for s in gate._slots if s.warm_owner == "A"]
            held = [s for s in gate._slots if s.held]
            self.assertEqual(len(reserved), 1)
            self.assertNotIn(reserved[0], held)  # C took a cold slot instead

    def test_head_evicts_live_reservation_of_waiting_owner(self):
        # once lapsed, a live reservation no longer blocks the head
        now = [100.0]
        gate = SlotSemaphore(2, max_warm_run=100, warm_grace=10.0, poll=POLL, clock=lambda: now[0])
        gate._slots[0] = _Slot(warm_owner="X", warm_until=99.0)   # lapsed
        gate._slots[1] = _Slot(warm_owner="GONE", warm_until=200.0)  # still live
        gate._queue.extend(["HEAD", "X"])
        with gate._cond:
            i, _ = gate._take("HEAD")
        self.assertEqual(i, 0)  # head takes the lapsed slot, even though X waits on it

    def _head_take(self, gate, owner):
        gate._queue.append(owner)
        try:
            with gate._cond:
                i, _ = gate._take(owner)
        finally:
            gate._queue.remove(owner)
        return i

    def test_head_evicts_earliest_released_cold_slot(self):
        # LRU among cold slots: evict the one whose owner left longest ago
        now = [10.0]
        gate = SlotSemaphore(2, max_warm_run=1, warm_grace=5.0, poll=POLL, clock=lambda: now[0])
        self.assertEqual(self._head_take(gate, "X"), 0)
        gate._release(0)  # cap reached -> cold, released at t=10
        now[0] = 20.0
        self.assertEqual(self._head_take(gate, "Y"), 1)  # never-used slot 1 before X's cold slot
        gate._release(1)  # cold, released at t=20
        self.assertEqual(self._head_take(gate, "HEAD"), 0)  # X's slot is the coldest
        self.assertEqual(gate._slots[0].warm_owner, "HEAD")

    def test_never_used_slot_taken_first(self):
        gate = SlotSemaphore(2, max_warm_run=1, warm_grace=5.0, poll=POLL)
        with gate.hold("X"):
            pass  # slot 0 held then released cold; slot 1 never used
        self.assertEqual(self._head_take(gate, "HEAD"), 1)  # untouched slot: no owner's cache to destroy

    def test_clear_warm_lets_the_head_take_at_once(self):
        now = [100.0]
        gate = SlotSemaphore(1, max_warm_run=100, warm_grace=10.0, poll=POLL, clock=lambda: now[0])
        gate._slots[0] = _Slot(warm_owner="A", warm_until=110.0, released_at=100.0)
        gate._queue.append("HEAD")
        with gate._cond:
            self.assertIsNone(gate._take("HEAD")[0])  # live reservation blocks the head
        gate.clear_warm("A")
        self.assertEqual(self._head_take(gate, "HEAD"), 0)  # despite 10s grace left

    def test_clear_warm_spares_held_and_foreign_slots(self):
        gate = SlotSemaphore(2, max_warm_run=100, warm_grace=10.0, poll=POLL)
        gate._slots[0] = _Slot(warm_owner="A", warm_until=110.0)  # A's ended run
        gate._slots[1] = _Slot(held=True, warm_owner="B", warm_until=110.0)  # B still holds
        gate.clear_warm("A")
        with gate._cond:
            self.assertIsNone(gate._slots[0].warm_owner)  # hint dropped: plain cold slot
            self.assertEqual(gate._slots[1].warm_owner, "B")  # held: untouched

    def test_head_waits_while_all_reservations_live(self):
        now = [100.0]
        gate = SlotSemaphore(2, max_warm_run=100, warm_grace=10.0, poll=POLL, clock=lambda: now[0])
        gate._slots[0] = _Slot(warm_owner="X", warm_until=200.0)
        gate._slots[1] = _Slot(warm_owner="Y", warm_until=210.0)
        gate._queue.append("HEAD")
        with gate._cond:
            i, retry_at = gate._take("HEAD")
        self.assertIsNone(i)  # nothing lapsed yet
        self.assertEqual(retry_at, 200.0)  # told to retry at the earliest lapse
        now[0] = 205.0  # X's reservation lapses
        with gate._cond:
            self.assertEqual(gate._take("HEAD")[0], 0)  # earliest-lapsed wins

    def _hold_marked(self, gate, order, owner):
        with gate.hold(owner):
            order.append(owner)


if __name__ == "__main__":
    unittest.main()
