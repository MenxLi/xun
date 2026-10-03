import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from xun import Agent, NullDisplay
from xun.display_abstract import DisplayAbstract, DisplayEvent
from xun.hooks import HookArgs
from xun.loop import _execute_step
from xun.workspace import Workspace


class _PromptDisplay(DisplayAbstract):
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.prompts: list[str] = []

    def on_event(self, event: DisplayEvent) -> None:
        ...

    def get_choice(self, request: DisplayAbstract.ChoiceRequest) -> str:
        self.prompts.append(request.prompt)
        return self.answers.pop(0) if self.answers else "No"


class CompletionRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.display = _PromptDisplay()
        self.attempts = 0
        self.agent = self._agent(self.display)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _agent(self, display: DisplayAbstract) -> Agent:
        agent = Agent(display=display, workspace=Workspace(workdir=Path(self._tmp.name)))
        agent.config.auto_confirm = False
        agent._openai_client = SimpleNamespace(   # type: ignore
            chat=SimpleNamespace(completions=SimpleNamespace(create=self._create))
        )
        return agent

    def _create(self, **kwargs) -> None:
        self.attempts += 1
        raise RuntimeError("provider unavailable")

    def _run(self, answers: list[str] = (), auto_confirm: bool = False) -> tuple[Exception, list[float]]:
        self.display.answers = list(answers)
        self.agent.config.auto_confirm = auto_confirm
        params = HookArgs.BeforeExecutionArgs(agent=self.agent, schema=None, max_iterations=1)
        with patch("xun.loop.time.sleep") as sleep:
            with self.assertRaises(RuntimeError) as raised:
                _execute_step(params, "call-id")
        return raised.exception, [call.args[0] for call in sleep.call_args_list]

    def test_declined_retry_raises_after_the_backoff_is_spent(self) -> None:
        _, backoffs = self._run(["No"])
        self.assertEqual(backoffs, [0.5, 1.0, 2.0])
        self.assertEqual(self.attempts, 4)
        self.assertEqual(self.display.prompts, ["Retry?"])

    def test_approved_retry_gets_the_full_backoff_again(self) -> None:
        _, backoffs = self._run(["Yes", "No"])
        self.assertEqual(backoffs, [0.5, 1.0, 2.0, 0.5, 1.0, 2.0])
        self.assertEqual(self.attempts, 8)
        self.assertEqual(self.display.prompts, ["Retry?", "Retry?"])

    def test_auto_confirm_cannot_approve_the_retry(self) -> None:
        _, backoffs = self._run(["No"], auto_confirm=True)
        self.assertEqual(backoffs, [0.5, 1.0, 2.0])
        self.assertEqual(self.attempts, 4)
        self.assertEqual(self.display.prompts, ["Retry?"])

    def test_headless_display_reraises_the_provider_error(self) -> None:
        # a headless display must surface the provider error, not NotImplementedError
        self.agent = self._agent(NullDisplay())
        raised, _ = self._run(auto_confirm=True)
        self.assertIn("provider unavailable", str(raised))
        self.assertEqual(self.attempts, 4)


if __name__ == "__main__":
    unittest.main()
