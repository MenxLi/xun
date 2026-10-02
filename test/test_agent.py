import gc
import unittest
import weakref
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch
from xun import Agent, NullDisplay
from xun.agent import AGENTS_SECTION
from xun.conversation_message import SystemPrompt
from xun.display_abstract import (
    AgentRunningEndEvent,
    AgentRunningStartEvent,
    ConfirmEvent,
    DisplayAbstract,
    DisplayEvent,
)
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

    def test_initialize_binds_display_and_is_idempotent(self) -> None:
        agent = self._new_agent()
        self.assertFalse(agent.is_initialized(agent))
        ready = agent.initialize()
        self.assertIs(ready, agent)
        self.assertTrue(agent.is_initialized(agent))
        self.assertIn(agent.identifier, agent.display.agents)
        self.assertIs(agent.initialize(), agent)

    def test_agents_md_is_loaded_at_initialize_and_removed_when_missing(self) -> None:
        path = self.workdir / "AGENTS.md"
        path.write_text("Follow local conventions.", encoding="utf-8")
        agent = self._new_agent().initialize()
        prompt = agent.conversation.messages[0]
        self.assertIsInstance(prompt, SystemPrompt)
        assert isinstance(prompt, SystemPrompt)
        self.assertIn("Follow local conventions.", prompt.persist_sections[AGENTS_SECTION])
        self.assertIn("Follow local conventions.", prompt.completion_param()["content"])

        path.unlink()
        agent.sync_project_instructions()
        self.assertNotIn(AGENTS_SECTION, prompt.persist_sections)

    def test_load_project_instructions_gates_agents_md(self) -> None:
        (self.workdir / "AGENTS.md").write_text("Follow local conventions.", encoding="utf-8")
        agent = self._new_agent()
        agent.config.load_project_instructions = False
        agent = agent.initialize()
        self.assertEqual(agent.conversation.messages, [])

        agent.config.load_project_instructions = True
        agent.sync_project_instructions()
        prompt = agent.conversation.messages[0]
        assert isinstance(prompt, SystemPrompt)
        self.assertIn("Follow local conventions.", prompt.persist_sections[AGENTS_SECTION])

        agent.config.load_project_instructions = False
        agent.sync_project_instructions()
        self.assertNotIn(AGENTS_SECTION, prompt.persist_sections)

    def test_agents_md_syncs_before_and_after_execute_even_on_error(self) -> None:
        path = self.workdir / "AGENTS.md"
        agent = self._new_agent().initialize()
        path.write_text("Before execution.", encoding="utf-8")

        def execute_with_edit(_params: object) -> str:
            prompt = agent.conversation.messages[0]
            assert isinstance(prompt, SystemPrompt)
            self.assertIn("Before execution.", prompt.persist_sections[AGENTS_SECTION])
            path.write_text("After execution.", encoding="utf-8")
            return "done"

        with patch("xun.agent.execution_loop", side_effect=execute_with_edit):
            self.assertEqual(agent.execute().unwrap(), "done")
        prompt = agent.conversation.messages[0]
        assert isinstance(prompt, SystemPrompt)
        self.assertIn("After execution.", prompt.persist_sections[AGENTS_SECTION])

        def execute_with_error(_params: object) -> str:
            path.unlink()
            raise RuntimeError("execution failed")

        with patch("xun.agent.execution_loop", side_effect=execute_with_error):
            self.assertTrue(agent.execute().is_err())
        self.assertNotIn(AGENTS_SECTION, prompt.persist_sections)

    def test_subagent_loads_shared_workdir_agents_md(self) -> None:
        parent = self._new_agent().initialize()
        (self.workdir / "AGENTS.md").write_text("Child instructions.", encoding="utf-8")
        child = Agent.inherit(parent).initialize()
        prompt = child.conversation.messages[0]
        assert isinstance(prompt, SystemPrompt)
        self.assertIn("Child instructions.", prompt.persist_sections[AGENTS_SECTION])

    def test_finalize_unbinds_display_and_is_idempotent(self) -> None:
        agent = self._new_agent().initialize()
        agent.finalize()
        self.assertNotIn(agent.identifier, agent.display.agents)
        agent.finalize()
        self.assertNotIn(agent.identifier, agent.display.agents)

    def test_context_manager_binds_and_unbinds(self) -> None:
        with self._new_agent() as agent:
            self.assertIn(agent.identifier, agent.display.agents)
        self.assertNotIn(agent.identifier, agent.display.agents)

    def test_guards_reject_uninitialized_or_finalized(self) -> None:
        # type-level: execute()/initialize() are state-gated; the casts test the runtime guards.
        agent = self._new_agent()
        res = cast("Agent[Agent.T.Init]", agent).execute()
        self.assertTrue(res.is_err())
        self.assertIn("not initialized", res.unwrap_err().error)

        finalized = cast("Agent[Agent.T.Final]", agent.initialize().finalize())
        self.assertTrue(Agent.is_finalized(finalized))
        self.assertFalse(Agent.is_initialized(finalized))
        self.assertTrue(cast("Agent[Agent.T.Init]", finalized).execute().is_err())
        with self.assertRaises(RuntimeError) as ctx:
            cast("Agent[Agent.T.Uninit]", finalized).initialize()
        self.assertIn("finalized", str(ctx.exception))

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

    def test_cancel_only_sets_event_while_running(self) -> None:
        # cancelling an idle agent must not set the (possibly shared) cancel event,
        # which would poison the next execution before it starts
        agent = self._new_agent().initialize()
        self.assertFalse(agent.cancel())
        self.assertFalse(agent.cancel_event.event.is_set())
        agent._running = True
        self.assertTrue(agent.cancel())
        self.assertTrue(agent.cancel_event.event.is_set())
        with self.assertRaises(CancelledError):
            agent.check_cancel()

    def test_running_events_bracket_execution_scopes(self) -> None:
        display = _RecordingDisplay()
        agent = Agent(display=display, workspace=Workspace(workdir=self.workdir)).initialize()

        with agent.cancellable_execution():
            with agent.cancellable_execution():
                self.assertTrue(agent.is_running)

        running_events = [
            event.payload for event in display.events
            if isinstance(event.payload, (AgentRunningStartEvent, AgentRunningEndEvent))
        ]
        self.assertEqual(
            [type(event) for event in running_events],
            [AgentRunningStartEvent, AgentRunningEndEvent],
        )
        self.assertFalse(agent.is_running)

        with self.assertRaisesRegex(RuntimeError, "failed"):
            with agent.cancellable_execution():
                raise RuntimeError("failed")

        self.assertIsInstance(display.events[-1].payload, AgentRunningEndEvent)
        self.assertFalse(agent.is_running)

    def test_confirmation_events_from_auto_confirm_and_user_choice(self) -> None:
        display = _RecordingDisplay()
        agent = Agent(display=display, workspace=Workspace(workdir=self.workdir))
        agent.config.auto_confirm = True
        agent = cast("Agent[Agent.T.Init]", agent)  # gated helpers need Init; runtime path is state-agnostic

        self.assertEqual(agent.get_choice("Proceed?", ["Yes", "No"], default="Yes").choice, "Yes")
        self.assertIsInstance(display.events[-1].payload, ConfirmEvent)
        self.assertEqual(display.events[-1].payload.source, "auto")

        agent.config.auto_confirm = False
        self.assertEqual(agent.get_choice("Proceed?", ["Yes", "No"]).choice, "No")
        self.assertIsInstance(display.events[-1].payload, ConfirmEvent)
        self.assertEqual(display.events[-1].payload.source, "user")
        self.assertEqual(display.events[-1].payload.choices, ["Yes", "No"])


if __name__ == "__main__":
    unittest.main()
