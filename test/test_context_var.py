import asyncio
import contextvars
import threading
import unittest


class RunCoroutineThreadsafeContextVarTest(unittest.TestCase):
    def setUp(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def test_run_coroutine_threadsafe_propagates_submission_context(self) -> None:
        current_value = contextvars.ContextVar("current_value", default="default")

        async def read_value() -> str:
            return current_value.get()

        current_value.set("caller-value")
        future = asyncio.run_coroutine_threadsafe(read_value(), self._loop)

        self.assertEqual(future.result(timeout=1), "caller-value")

    def test_run_coroutine_threadsafe_captures_context_at_submission_time(self) -> None:
        current_value = contextvars.ContextVar("current_value", default="default")
        gate = threading.Event()
        release = threading.Event()

        async def read_after_release() -> str:
            gate.set()
            await asyncio.to_thread(release.wait)
            return current_value.get()

        current_value.set("value-at-submit")
        future = asyncio.run_coroutine_threadsafe(read_after_release(), self._loop)

        self.assertTrue(gate.wait(timeout=1))
        current_value.set("value-after-submit")
        release.set()

        self.assertEqual(future.result(timeout=1), "value-at-submit")


if __name__ == "__main__":
    unittest.main()