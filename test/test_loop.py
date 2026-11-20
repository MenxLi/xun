import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from xun import Agent, NullDisplay
from xun.core.display_abstract import DisplayAbstract, DisplayEvent, ModelMessageEvent
from xun.core.hooks import HookArgs
from xun.core.loop import _execute_step, execution_loop
from xun.core.toolbox import ToolBox
from xun.core.types import ConclusiveToolResult
from xun.core.workspace import Workspace


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
        agent.openai_client = SimpleNamespace(   # type: ignore[assignment]
            chat=SimpleNamespace(completions=SimpleNamespace(create=self._create))
        )
        return agent

    def _create(self, **kwargs) -> None:
        self.attempts += 1
        raise RuntimeError("provider unavailable")

    def _run(self, answers: list[str] = [], auto_confirm: bool = False) -> tuple[Exception, list[float]]:
        self.display.answers = list(answers)
        self.agent.config.auto_confirm = auto_confirm
        params = HookArgs.BeforeExecutionArgs(agent=cast("Agent[Agent.T.Init]", self.agent), schema=None, max_iterations=1)
        with patch("xun.core.loop.time.sleep") as sleep:
            with self.assertRaises(RuntimeError) as raised:
                _execute_step(params, 1)
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


class _RecordingDisplay(DisplayAbstract):
    def __init__(self) -> None:
        self.received: list[DisplayEvent] = []

    def on_event(self, event: DisplayEvent) -> None:
        self.received.append(event)

    def get_choice(self, request: DisplayAbstract.ChoiceRequest) -> str:
        return "No"


def _tool_call_chunk(tool_name: str, arguments: str):
    delta = SimpleNamespace(
        content=None,
        tool_calls=[SimpleNamespace(
            index=0,
            id="call-1",
            type="function",
            function=SimpleNamespace(name=tool_name, arguments=arguments),
        )],
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)

def _usage_chunk(total_tokens: int):
    usage = SimpleNamespace(
        completion_tokens=10,
        prompt_tokens=total_tokens - 10,
        total_tokens=total_tokens,
        prompt_tokens_details=None,
    )
    return SimpleNamespace(choices=[], usage=usage)


class ConclusiveToolResultTest(unittest.TestCase):
    """A tool returning ConclusiveToolResult must close the run and carry meta to the closing message."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.display = _RecordingDisplay()
        self.agent = Agent(
            display=self.display,
            workspace=Workspace(workdir=Path(self._tmp.name)),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, tool, name: str = "finish", tokens: int = 123) -> tuple[str, list[DisplayEvent]]:
        self.agent.openai_client = SimpleNamespace(  # type: ignore[assignment]
            chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kwargs: iter([_tool_call_chunk(name, '{"summary": "done"}'), _usage_chunk(tokens)])
            ))
        )
        self.agent.toolbox = ToolBox().register(tool)
        params = HookArgs.BeforeExecutionArgs(agent=cast("Agent[Agent.T.Init]", self.agent), schema=None, max_iterations=1)
        result = execution_loop(params)
        return cast(str, result), list(self.display.received)

    def test_conclusive_result_closes_the_run_with_meta_and_paired_history(self) -> None:
        def finish(summary: str) -> ConclusiveToolResult:
            """Finish the task."""
            return ConclusiveToolResult(content=summary, meta={"artifacts": [{"path": "out/report.md", "dir": False, "note": "report"}]})

        result, received = self._run(finish)
        self.assertEqual(result, "done")

        names = [event.name for event in received]
        self.assertEqual(names, ["ModelWorkingEvent", "ToolCallEvent", "ToolResultEvent", "ModelMessageEvent"])

        closing = received[-1].payload
        assert isinstance(closing, ModelMessageEvent)
        self.assertTrue(closing.conclusive)
        self.assertEqual(closing.content, "done")
        self.assertEqual(closing.total_tokens, 123)
        self.assertEqual(closing.meta, {"artifacts": [{"path": "out/report.md", "dir": False, "note": "report"}]})

        # closing message sits outside the tool-call span so displays render it top-level
        self.assertIsNone(received[-1].span.tool_call)

        # history ends at the paired tool result; the closing message is display-only
        history = self.agent.conversation.messages
        roles = [message.role for message in history]
        self.assertEqual(roles[-2:], ["assistant", "tool"])
        self.assertIn("artifacts", history[-1].content_str)
        self.assertIn("done", history[-1].content_str)

    def test_tool_error_does_not_end_the_run(self) -> None:
        def failing(summary: str) -> ConclusiveToolResult:
            """Fail on purpose."""
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError) as raised:
            self._run(failing, name="failing", tokens=50)
        self.assertIn("Maximum tool call iterations", str(raised.exception))
        self.assertNotIn("ModelMessageEvent", [event.name for event in self.display.received])


if __name__ == "__main__":
    unittest.main()
