import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from xun import Agent, AgentState, ErrorInfo, HookArgs, Hooks, JsonEntry, NullDisplay, Result, RuntimeEntry, Workspace
from xun.core.display_event import AgentInfo, DisplayEvent, InfoEvent
from xun.core.tracing import SpanSnapshot
from xun.core.types import CancelledError
from xun.core.workspace import DeferredTempDirectory
from xun.session import (
    AgentStore,
    CommandInstruction,
    DBEventStore,
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


class AgentStoreTest(unittest.TestCase):
    def test_save_and_load_restore_name_conversation_state_and_workspace(self) -> None:
        with TemporaryDirectory() as home, TemporaryDirectory() as workdir, TemporaryDirectory() as tempdir:
            store = AgentStore(Path(home) / "session")
            self.assertFalse(store.exists())
            agent = Agent(
                name="saver",
                display=NullDisplay(),
                workspace=Workspace(workdir=Path(workdir), tempdir=DeferredTempDirectory(Path(tempdir))),
            ).initialize()
            agent.conversation.add_user_message("durable")
            agent.state["persisted"] = JsonEntry("kept-value")
            agent.state["ephemeral"] = RuntimeEntry(42)
            store.save(agent)
            self.assertTrue(store.exists())

            other = Agent(name="other", display=NullDisplay())
            other.state["persisted"] = JsonEntry("to-be-replaced")
            other.state["live"] = RuntimeEntry("survivor")
            notes = store.load(other)
            self.assertEqual(notes, [])
            self.assertEqual(other.name, "saver")
            self.assertEqual(other.identifier, agent.identifier)
            param = cast(dict, other.conversation.messages[-1].completion_param())
            self.assertEqual(param["content"], "durable")
            self.assertEqual(other.state["persisted"], JsonEntry("kept-value"))
            self.assertEqual(other.state["live"], RuntimeEntry("survivor"))  # runtime entries survive
            self.assertNotIn("ephemeral", other.state)  # runtime entries are never stored
            self.assertEqual(other.workspace.workdir, Path(workdir))
            self.assertEqual(other.workspace.tempdir.path, Path(tempdir))

            other = other.initialize()
            self.assertEqual(other.display.agents, {other.identifier: other})  # first bind uses the restored id

    def test_missing_paths_degrade_with_notes(self) -> None:
        with TemporaryDirectory() as home:
            with TemporaryDirectory() as workdir, TemporaryDirectory() as tempdir:
                store = AgentStore(Path(home) / "session")
                agent = Agent(
                    display=NullDisplay(),
                    workspace=Workspace(workdir=Path(workdir), tempdir=DeferredTempDirectory(Path(tempdir))),
                ).initialize()
                store.save(agent)

            # both saved paths are gone with their contexts, the snapshot remains
            live_dir = TemporaryDirectory()
            self.addCleanup(live_dir.cleanup)
            other = Agent(display=NullDisplay(), workspace=Workspace(workdir=Path(live_dir.name)))
            notes = store.load(other)
            self.assertEqual(len(notes), 2)
            self.assertEqual(other.workspace.workdir, Path(live_dir.name))
            self.assertIsNone(other.workspace.tempdir.external_path)


class DBEventStoreTest(unittest.TestCase):
    @staticmethod
    def _event(agent_id: str, message: str) -> DisplayEvent:
        return DisplayEvent(
            name="InfoEvent",
            agent=AgentInfo(name="a", identifier=agent_id, workdir=Path("/w")),
            span=SpanSnapshot(),
            payload=InfoEvent(message=message),
        )

    def test_round_trip_pagination_and_clear(self) -> None:
        with TemporaryDirectory() as home:
            store = DBEventStore(Path(home) / "events.db")
            self.addCleanup(store.close)
            store.add(self._event("one", "hello"))
            store.add(self._event("two", "other agent"))
            store.add(self._event("one", "world"))

            self.assertEqual(store.count(), 3)
            self.assertEqual(store.count("one"), 2)
            self.assertEqual(store.agent_ids(), ["one", "two"])
            self.assertEqual([e.payload.message for e in store.select("one")], ["hello", "world"])
            self.assertEqual([e.payload.message for e in store.select("one", from_latest=True, limit=1)], ["world"])
            self.assertEqual([e.payload.message for e in store.select("one", offset=1)], ["world"])

            store.clear("one")
            self.assertEqual(store.count(), 1)
            store.clear()
            self.assertEqual(store.count(), 0)

    def test_events_survive_reopen(self) -> None:
        with TemporaryDirectory() as home:
            path = Path(home) / "events.db"
            store = DBEventStore(path)
            store.add(self._event("one", "before restart"))
            store.close()

            reopened = DBEventStore(path)
            self.addCleanup(reopened.close)
            events = reopened.select("one")
            self.assertEqual([e.payload.message for e in events], ["before restart"])
            self.assertEqual(events[0].agent.name, "a")

    def test_buffer_size_prunes_oldest(self) -> None:
        with TemporaryDirectory() as home:
            store = DBEventStore(Path(home) / "events.db")
            self.addCleanup(store.close)
            for i in range(5):
                store.add(self._event("one", str(i)))
            store.set_buffer_size(2)
            self.assertEqual(store.count(), 2)
            self.assertEqual([e.payload.message for e in store.select()], ["3", "4"])

    def test_manual_commit_batches_until_flush(self) -> None:
        with TemporaryDirectory() as home:
            path = Path(home) / "events.db"
            store = DBEventStore(path, manual_commit=True)
            reader = DBEventStore(path)
            self.addCleanup(reader.close)
            store.add(self._event("one", "pending"))
            self.assertEqual(store.count(), 1)  # same connection sees its own writes
            self.assertEqual(reader.count(), 0)  # not durable yet
            store.clear("nobody")  # incidental writes must not shadow-commit pending events
            self.assertEqual(reader.count(), 0)
            store.flush()
            self.assertEqual(reader.count(), 1)
            store.add(self._event("one", "on close"))
            store.close()  # close flushes
            self.assertEqual(reader.count("one"), 2)


if __name__ == "__main__":
    unittest.main()
