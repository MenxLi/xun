import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from xun import Agent, NullDisplay
from xun.conversation import Conversation, CompactionCounter
from xun.compact import AutoCompactor, CompactorAbstract
from xun.hooks import HookArgs, Hooks
from xun.workspace import Workspace


KEEP_RECENT = 16


def _conversation_with_tool_chain(tool_rounds: int, tool_size: int = 500) -> Conversation:
    conversation = Conversation()
    conversation.set_system_message_content("sys")
    conversation.add_user_message("do the thing")
    for i in range(tool_rounds):
        conversation.messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call_{i}", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        })
        conversation.messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": "x" * tool_size})
    return conversation


class ConversationCompactTest(unittest.TestCase):
    def test_cut_at_last_user_message_when_tail_fits(self) -> None:
        conversation = Conversation()
        conversation.set_system_message_content("sys")
        conversation.add_user_message("first")
        conversation.messages.append({"role": "assistant", "content": "a" * 100})
        conversation.add_user_message("second")

        self.assertTrue(conversation.compact(lambda messages: "SUMMARY", keep_recent=KEEP_RECENT))
        self.assertEqual([m["role"] for m in conversation.messages], ["system", "user"])
        self.assertIn("SUMMARY", conversation.messages[0].get("content") or "")
        # summary supersedes cheap rounds: tool count resets, summary count advances
        self.assertEqual(conversation.compaction_counter.tool_rounds, 0)
        self.assertEqual(conversation.compaction_counter.summary_rounds, 1)

    def test_long_tool_chain_compacts_to_bounded_window(self) -> None:
        conversation = _conversation_with_tool_chain(tool_rounds=40)

        self.assertTrue(conversation.compact(lambda messages: "BIG", keep_recent=KEEP_RECENT))
        self.assertLessEqual(len(conversation.messages), KEEP_RECENT + 2)  # system + carried user + retained tail
        # the kept tail never opens with an orphaned tool result
        self.assertNotEqual(conversation.messages[1]["role"], "tool")
        # every tool result in the tail keeps its owning assistant message
        tail = conversation.messages[1:]
        for index, message in enumerate(tail):
            if message["role"] == "tool":
                self.assertTrue(tail[index - 1].get("tool_calls"))

    def test_compact_keeps_a_user_message_for_deep_tool_chains(self) -> None:
        # a single user instruction under a long tool chain: the last user message is
        # too deep to be the cut point, but providers reject bodies without a user
        # query, so it must survive into the kept tail
        conversation = _conversation_with_tool_chain(tool_rounds=40)

        self.assertTrue(conversation.compact(lambda messages: "BIG", keep_recent=KEEP_RECENT))
        self.assertIn("user", [m["role"] for m in conversation.messages])

    def test_nothing_to_condense_leaves_history_untouched(self) -> None:
        conversation = Conversation()
        conversation.set_system_message_content("sys")
        conversation.add_user_message("hi")

        self.assertFalse(conversation.compact(lambda messages: "S", keep_recent=KEEP_RECENT))
        self.assertEqual([m["role"] for m in conversation.messages], ["system", "user"])

    def test_failed_summary_rolls_back_and_clears_token_count(self) -> None:
        conversation = _conversation_with_tool_chain(tool_rounds=3)
        conversation.total_tokens = 12345
        # give the cut point something to condense, so the summarizer actually runs
        conversation.messages.append({"role": "assistant", "content": "done"})
        conversation.add_user_message("keep going")
        before = list(conversation.messages)

        self.assertFalse(conversation.compact(lambda messages: None, keep_recent=KEEP_RECENT))
        self.assertEqual(conversation.messages, before)
        self.assertEqual(conversation.total_tokens, 12345)

    def test_successful_summary_invalidates_token_count(self) -> None:
        conversation = _conversation_with_tool_chain(tool_rounds=3)
        conversation.total_tokens = 99999
        # give the cut point something to condense: a turn before the last user message
        conversation.messages.append({"role": "assistant", "content": "done"})
        conversation.add_user_message("keep going")

        self.assertTrue(conversation.compact(lambda messages: "S", keep_recent=KEEP_RECENT))
        self.assertIsNone(conversation.total_tokens)


class CompactionCounterTest(unittest.TestCase):
    def test_counter_persists_round_trip(self) -> None:
        conversation = Conversation()
        conversation.compaction_counter = CompactionCounter(tool_rounds=1, summary_rounds=2)

        restored = Conversation()
        restored.loads(conversation.dumps())
        self.assertEqual(restored.compaction_counter.tool_rounds, 1)
        self.assertEqual(restored.compaction_counter.summary_rounds, 2)

    def test_counter_defaults_for_legacy_files(self) -> None:
        restored = Conversation()
        restored.loads(json.dumps({"messages": [], "tokens_used": 5}))
        self.assertEqual(restored.compaction_counter, CompactionCounter())

    def test_clear_resets_counter(self) -> None:
        conversation = Conversation()
        conversation.compaction_counter.tool_rounds = 3
        conversation.clear()
        self.assertEqual(conversation.compaction_counter, CompactionCounter())


class AutoCompactionTest(unittest.TestCase):
    def _agent(self, threshold: int, total_tokens: int | None) -> MagicMock:
        agent = MagicMock()
        agent.config.auto_compact.enabled = True
        agent.config.auto_compact.token_threshold = threshold
        agent.conversation = _conversation_with_tool_chain(tool_rounds=30)
        agent.conversation.total_tokens = total_tokens
        return agent

    def _summary(self, agent: MagicMock) -> MagicMock:
        # _auto_compact resolves compact_conversation through the module
        # namespace, so that is the attribute to patch.
        summary = MagicMock()
        summary.side_effect = lambda *a, **k: agent.conversation.compact(
            lambda messages: "SUMMARY",
            keep_recent=k.get("keep_recent", KEEP_RECENT),
        )
        return summary

    def test_disabled_never_compacts(self) -> None:
        agent = self._agent(1_000, 9_999_999)
        agent.config.auto_compact.enabled = False
        with patch("xun.compact.compact_conversation", self._summary(agent)) as compact_conversation:
            AutoCompactor().auto_compact(agent)
        compact_conversation.assert_not_called()

    def test_under_threshold_never_compacts(self) -> None:
        agent = self._agent(100_000, 5_000)
        with patch("xun.compact.compact_conversation", self._summary(agent)) as compact_conversation:
            AutoCompactor().auto_compact(agent)
        compact_conversation.assert_not_called()

    def test_no_escalation_when_cheap_pass_brings_estimate_under_threshold(self) -> None:
        # the tool chain's reclaim fraction is large enough that the estimated token
        # count falls below the threshold: the cheap pass alone suffices
        agent = self._agent(100_000, 105_000)
        with patch("xun.compact.compact_conversation", self._summary(agent)) as compact_conversation:
            AutoCompactor().auto_compact(agent)
        self.assertEqual(agent.conversation.compaction_counter.tool_rounds, 1)
        compact_conversation.assert_not_called()

    def test_escalates_when_reclaim_leaves_estimate_over_threshold(self) -> None:
        # huge stale count vs. tiny threshold: even a big reclaim cannot get under it
        agent = self._agent(1_000, 9_999_999)
        with patch("xun.compact.compact_conversation", self._summary(agent)) as compact_conversation:
            AutoCompactor().auto_compact(agent)
        self.assertEqual(compact_conversation.call_count, 3)
        agent.error.assert_called_once()
        self.assertIn("no further progress possible", agent.error.call_args.args[0])

    def test_escalates_after_enough_cheap_rounds(self) -> None:
        agent = self._agent(100_000, 105_000)
        compactor = AutoCompactor()
        with patch("xun.compact.compact_conversation", self._summary(agent)) as compact_conversation:
            for _ in range(compactor.escalation_rounds):
                compactor.auto_compact(agent)
        compact_conversation.assert_called()
        self.assertEqual(agent.conversation.compaction_counter.tool_rounds, 0)
        self.assertEqual(agent.conversation.compaction_counter.summary_rounds, 1)

    def test_escalates_immediately_when_cheap_pass_reclaims_nothing(self) -> None:
        agent = self._agent(1_000, 9_999_999)
        agent.conversation.messages = agent.conversation.messages[:4]  # below keep_max, nothing to reclaim
        with patch("xun.compact.compact_conversation", self._summary(agent)) as compact_conversation:
            AutoCompactor().auto_compact(agent)
        compact_conversation.assert_called()

    def test_no_retrigger_while_token_count_unknown(self) -> None:
        agent = self._agent(1_000, None)
        with patch("xun.compact.compact_conversation", self._summary(agent)) as compact_conversation:
            AutoCompactor().auto_compact(agent)
        compact_conversation.assert_not_called()

    def test_summary_failure_is_reported_without_aborting_execution(self) -> None:
        agent = self._agent(1_000, 9_999_999)
        agent.conversation.compaction_counter.tool_rounds = AutoCompactor().escalation_rounds - 1
        failed = MagicMock(side_effect=RuntimeError("summary failed"))
        with patch("xun.compact.compact_conversation", failed):
            AutoCompactor().auto_compact(agent)

        agent.error.assert_called_once_with("Auto-compaction failed: summary failed")


class CompactorInstallTest(unittest.TestCase):
    """initialize() wires the compactor component into the hooks."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _agent(self) -> Agent:
        return Agent(display=NullDisplay(), workspace=Workspace(workdir=Path(self._tmp.name)))

    def test_hooks_are_clean_before_initialize(self) -> None:
        agent = self._agent()
        self.assertEqual(len(agent.hooks.before_execution_step._callbacks), 0)

    def test_initialize_installs_the_compactor(self) -> None:
        agent = self._agent().initialize()
        # one callback for the default Compactor
        self.assertEqual(len(agent.hooks.before_execution_step._callbacks), 1)
        # initialize is idempotent, so the install happens at most once
        agent.initialize()
        self.assertEqual(len(agent.hooks.before_execution_step._callbacks), 1)

    def test_subagent_gets_its_own_compactor(self) -> None:
        child = Agent.inherit(self._agent())
        self.assertIsInstance(child.compactor, AutoCompactor)
        child.initialize()
        self.assertEqual(len(child.hooks.before_execution_step._callbacks), 1)

    def test_install_wires_the_step_hook(self) -> None:
        hooks = Hooks()
        AutoCompactor().install(hooks)
        self.assertEqual(len(hooks.before_execution_step._callbacks), 1)

    def test_abstract_contract(self) -> None:
        with self.assertRaises(TypeError):
            CompactorAbstract()  # type: ignore[abstract]

        class NoopCompactor(CompactorAbstract):
            def auto_compact(self, agent) -> None:
                pass

        hooks = Hooks()
        NoopCompactor().install(hooks)
        self.assertEqual(len(hooks.before_execution_step._callbacks), 1)

    def test_hook_delegates_to_auto_compact(self) -> None:
        agent = MagicMock()
        hooks = Hooks()
        compactor = AutoCompactor()
        compactor.install(hooks)
        with patch.object(compactor, "auto_compact") as auto_compact:
            hooks.before_execution_step.invoke(HookArgs.BeforeExecutionStepArgs(agent=agent))
        auto_compact.assert_called_once_with(agent)

    def test_hook_reports_failures_without_raising(self) -> None:
        # HookRegistry swallows hook exceptions, so the hook must log them itself
        agent = MagicMock()
        hooks = Hooks()
        compactor = AutoCompactor()
        compactor.install(hooks)
        with patch.object(compactor, "auto_compact", side_effect=RuntimeError("boom")):
            hooks.before_execution_step.invoke(HookArgs.BeforeExecutionStepArgs(agent=agent))
        agent.error.assert_called_once_with("Compaction hook failed: boom")


if __name__ == "__main__":
    unittest.main()
