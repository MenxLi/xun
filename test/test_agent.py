import gc
import unittest
import weakref
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from xun import Agent, NullDisplay
from xun.display_abstract import ConfirmEvent, DisplayAbstract, DisplayEvent, InfoEvent
from xun.types import CancelledError
from xun.workspace import Workspace


class _RecordingDisplay(DisplayAbstract):
    def __init__(self) -> None:
        self.events: list[DisplayEvent] = []

    def on_event(self, event: DisplayEvent) -> None:
        self.events.append(event)

    def get_choice(self, request: DisplayAbstract.ChoiceRequest) -> str:
        return "No"


class AgentLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.workdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _new_agent(self) -> Agent:
        return Agent(display=NullDisplay(), workspace=Workspace(workdir=self.workdir))

    def test_constructed_agent_is_not_initialized(self) -> None:
        agent = self._new_agent()
        self.assertFalse(agent.is_initialized(agent))
        self.assertNotIn(agent.identifier, agent.display.agents)

    def test_initialize_binds_display_and_returns_self(self) -> None:
        agent = self._new_agent()
        ready = agent.initialize()
        self.assertIs(ready, agent)
        self.assertTrue(agent.is_initialized(agent))
        self.assertIn(agent.identifier, agent.display.agents)

    def test_initialize_is_idempotent(self) -> None:
        agent = self._new_agent()
        agent.initialize()
        self.assertIs(agent.initialize(), agent)
        self.assertIn(agent.identifier, agent.display.agents)

    def test_finalize_unbinds_display(self) -> None:
        agent = self._new_agent()
        initialized = agent.initialize()
        initialized.finalize()
        self.assertNotIn(agent.identifier, agent.display.agents)

    def test_finalize_is_idempotent(self) -> None:
        agent = self._new_agent()
        initialized = agent.initialize()
        initialized.finalize()
        initialized.finalize()
        self.assertNotIn(agent.identifier, agent.display.agents)

    def test_finalize_without_initialize_is_noop(self) -> None:
        # type-level: finalize() is not callable on Agent[T.Uninit]... it takes Agent[T.Alive],
        # so it IS callable here; the cast tests that a finalized agent stays a no-op path.
        agent = self._new_agent()
        agent.finalize()
        self.assertNotIn(agent.identifier, agent.display.agents)

    def test_context_manager_binds_and_unbinds(self) -> None:
        with self._new_agent() as agent:
            self.assertIn(agent.identifier, agent.display.agents)
        self.assertNotIn(agent.identifier, agent.display.agents)

    def test_execute_before_initialize_returns_error_result(self) -> None:
        # type-level: execute() is not callable on Agent[T.Uninit]; the cast
        # here tests that the runtime guard still yields a proper Err result.
        agent = self._new_agent()
        res = cast("Agent[Agent.T.Init]", agent).execute()
        self.assertTrue(res.is_err())
        self.assertIn("not initialized", res.unwrap_err().error)

    def test_gc_of_initialized_agent_collects(self) -> None:
        # the display holds a strong reference while bound; once both drop, the
        # agent must be collectable (weakref finalizer does not leak it)
        agent = self._new_agent()
        agent.initialize()
        display = agent.display
        weak = weakref.ref(agent)
        del agent
        del display
        gc.collect()
        self.assertIsNone(weak())

    def test_finalized_agent_cannot_execute_or_reinitialize(self) -> None:
        agent = self._new_agent()
        finalized = agent.initialize().finalize()
        self.assertTrue(Agent.is_finalized(finalized))
        self.assertFalse(Agent.is_initialized(finalized))
        # type-level: execute()/initialize() are not callable on Agent[T.Final];
        # the casts test the runtime guards.
        res = cast("Agent[Agent.T.Init]", finalized).execute()
        self.assertTrue(res.is_err())
        with self.assertRaises(RuntimeError) as ctx:
            cast("Agent[Agent.T.Uninit]", finalized).initialize()
        self.assertIn("finalized", str(ctx.exception))

    def test_cancel_on_idle_agent_is_noop(self) -> None:
        # cancelling an idle agent must not set the (possibly shared) cancel event,
        # which would poison the next execution before it starts
        agent = self._new_agent().initialize()
        self.assertFalse(agent.is_running)
        self.assertFalse(agent.cancel())
        self.assertFalse(agent.cancel_event.event.is_set())

    def test_cancel_while_running_sets_event(self) -> None:
        agent = self._new_agent().initialize()
        agent._running = True
        self.assertTrue(agent.cancel())
        self.assertTrue(agent.cancel_event.event.is_set())
        with self.assertRaises(CancelledError):
            agent.check_cancel()

    def test_auto_confirm_emits_confirmation_event(self) -> None:
        display = _RecordingDisplay()
        agent = Agent(display=display, workspace=Workspace(workdir=self.workdir))
        agent.config.auto_confirm = True

        self.assertEqual(agent.get_choice("Proceed?", ["Yes", "No"], default="Yes").choice, "Yes")
        self.assertIsInstance(display.events[-1].payload, ConfirmEvent)
        self.assertEqual(display.events[-1].payload.source, "auto")
        self.assertEqual(display.events[-1].payload.choices, ["Yes", "No"])
        self.assertNotIsInstance(display.events[-1].payload, InfoEvent)

    def test_user_choice_emits_confirmation_event(self) -> None:
        display = _RecordingDisplay()
        agent = Agent(display=display, workspace=Workspace(workdir=self.workdir))

        self.assertEqual(agent.get_choice("Proceed?", ["Yes", "No"]).choice, "No")
        self.assertIsInstance(display.events[-1].payload, ConfirmEvent)
        self.assertEqual(display.events[-1].payload.source, "user")
        self.assertEqual(display.events[-1].payload.choices, ["Yes", "No"])


if __name__ == "__main__":
    unittest.main()
