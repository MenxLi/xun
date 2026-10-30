import unittest
from typing import cast

from xun import Agent, AgentState, ErrorInfo, HookArgs, Hooks, Result
from xun.core.types import CancelledError
from xun.session import (
    CommandInstruction,
    MessageInstruction,
    SteeringStore,
    UserInstruction,
    execute_instruction,
    input_to_instruction,
    install_steering,
    run_command,
)


class InputParsingTest(unittest.TestCase):
    def test_message_parsing(self) -> None:
        self.assertEqual(
            input_to_instruction("[image:https://example.com/cat.png image:x] compare them"),
            MessageInstruction(content="compare them", images=["https://example.com/cat.png", "x"]),
        )
        self.assertEqual(input_to_instruction("[note:todo] keep"), MessageInstruction(content="[note:todo] keep"))
        self.assertEqual(input_to_instruction("\\/no-slash"), MessageInstruction(content="/no-slash"))

    def test_command_parsing(self) -> None:
        self.assertEqual(input_to_instruction("/compact toolcall"), CommandInstruction(command="compact", args="toolcall"))
        self.assertEqual(input_to_instruction("/help"), CommandInstruction(command="help", args=None))


class _FakeAgent:
    """Stands in for the agent surface the session layer drives."""

    def __init__(self, result: Result | Exception = Result.Ok(None)) -> None:
        self._outcome = result
        self.instructed: list[tuple[str, list[str] | None]] = []
        self.commands: list[tuple[str, str | None]] = []
        self.errors: list[str] = []
        self.execute_count = 0

    def instruct(self, content, images=None):
        self.instructed.append((content, list(images) if images else None))
        return self

    def execute(self):
        self.execute_count += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    def execute_command(self, name, arguments=None):
        self.commands.append((name, arguments))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        if name == "retry":
            # mirror the real retry protocol: the handler returns True to request a follow-up run
            return Result.Ok(True)
        return self._outcome

    def display_error(self, message):
        self.errors.append(message)

    def as_agent(self):
        return cast("Agent[Agent.T.Init]", self)


class DispatchTest(unittest.TestCase):
    def test_message_reports_execution_error(self) -> None:
        agent = _FakeAgent(Result.Err(ErrorInfo(error="boom", details="")))
        execute_instruction(agent.as_agent(), MessageInstruction(content="hi"))
        self.assertEqual(agent.instructed, [("hi", None)])
        self.assertEqual(agent.errors, ["Error executing instruction: boom"])

    def test_message_failure_is_reported_not_raised(self) -> None:
        def bad_instruct(*_args, **_kwargs):
            raise ValueError("bad input")

        agent = _FakeAgent()
        agent.instruct = bad_instruct
        execute_instruction(agent.as_agent(), MessageInstruction(content="hi"))
        self.assertEqual(agent.errors, ["Error executing instruction: bad input"])

    def test_cancelled_error_propagates(self) -> None:
        agent = _FakeAgent(CancelledError("user cancelled"))
        with self.assertRaises(CancelledError):
            execute_instruction(agent.as_agent(), MessageInstruction(content="hi"))

    def test_retry_signal_triggers_one_follow_up(self) -> None:
        agent = _FakeAgent()
        execute_instruction(agent.as_agent(), CommandInstruction(command="retry"))
        self.assertEqual(agent.commands, [("retry", None)])
        self.assertEqual(agent.execute_count, 1)

    def test_plain_command_never_executes_again(self) -> None:
        agent = _FakeAgent()
        run_command(agent.as_agent(), "compact", "toolcall")
        self.assertEqual(agent.execute_count, 0)

    def test_command_error_is_reported(self) -> None:
        agent = _FakeAgent(Result.Err(ErrorInfo(error="nope", details="")))
        run_command(agent.as_agent(), "tools", None)
        self.assertEqual(agent.errors, ["Error executing command: nope"])


class SteeringTest(unittest.TestCase):
    class _SteerableAgent:
        def __init__(self) -> None:
            self.hooks = Hooks()
            self.state = AgentState()
            self.instructed: list[tuple[str, list[str] | None]] = []

        def instruct(self, content, images=None):
            self.instructed.append((content, list(images) if images else None))
            return self

        def as_agent(self):
            return cast("Agent[Agent.T.Init]", self)

    def _flush(self, agent: _SteerableAgent) -> HookArgs.AfterExecutionStepArgs:
        args = HookArgs.AfterExecutionStepArgs(agent=agent.as_agent(), should_continue=False)
        agent.hooks.after_execution_step.invoke(args)
        return args

    def test_flush_injects_continues_and_notifies(self) -> None:
        agent = self._SteerableAgent()
        consumed: list[list[str]] = []
        install_steering(agent.as_agent(), on_consumed=lambda keys: consumed.append(list(keys)))

        key = SteeringStore.of(agent.as_agent()).add(UserInstruction(content="turn left", images=[]))
        args = self._flush(agent)

        self.assertEqual(agent.instructed, [("turn left", None)])
        self.assertTrue(args.should_continue)  # the loop must keep running to answer the steering
        self.assertEqual(consumed, [[key]])
        self.assertEqual(SteeringStore.of(agent.as_agent()).snapshot(), [])

    def test_idle_buffer_stops_hook_silently(self) -> None:
        agent = self._SteerableAgent()
        install_steering(agent.as_agent(), on_consumed=lambda _keys: self.fail("should not fire"))
        args = self._flush(agent)
        self.assertFalse(args.should_continue)
        self.assertEqual(agent.instructed, [])


if __name__ == "__main__":
    unittest.main()
