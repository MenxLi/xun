"""
Conversation compaction. 
`CompactorAbstract` is the agent component wired into `hooks` by `initialize()`;
`AutoCompactor` is the default strategy. `config.auto_compact.enabled` is the off switch.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Optional

from .types import CancelledError

if TYPE_CHECKING:
    from .agent import Agent
    from .hooks import HookArgs, Hooks

CONDENSE_PROMPT = """\
Condense the conversation above into a compact, structured summary that preserves all critical context for seamless continuation.
Your output will be used as a system message to inform the assistant of the conversation history, so it should be concise yet comprehensive enough for the assistant to understand the context and continue the conversation without losing important information.

RULES:
- PRESERVE SYSTEM MESSAGES: If any `system` role messages exist in the history, extract and include their key instructions/constraints in the "System Context" section. 
- PRESERVE: user goals, explicit preferences, factual claims, decisions made, pending tasks, open questions, and any constraints or rules established.
- DISCARD: greetings, small talk, filler, repeated statements, and conversational noise.
- GROUP by topic if it improves clarity, but maintain logical flow.
- Keep total output under 1024 tokens. If uncertain about a detail, mark it as "unconfirmed".
- OUTPUT in markdown format with the following field, do not add any other extra comment or explanation:

SCHEMA (in markdown format):
- system_context: key instructions or constraints from system messages (if any)
- overview: 1-2 sentence high-level summary of conversation purpose & current state
- key_facts: list of important facts mentioned
- user_preferences: list of user preferences
- decisions: list of decisions made
- pending_tasks: list of pending tasks
- open_questions: list of open questions
- tone_context: brief note on communication style or constraints (e.g., "formal", "prefers bullet points", "avoid technical jargon")
"""

COMPACTED_SYSTEM_PROMPT = """\
You are an assistant having a conversation with a user. Earlier conversation history has been compacted into the summary below:

{summary}

---
Context management notes:
- The most recent user message is always preserved verbatim and is authoritative for the current task. Messages around it may change: earlier turns are replaced by the summary above, and older tool results after it may be trimmed. 
- Older tool results may appear as [Compacted, ID: ...] placeholders. When such content is still needed, call `extract_compacted_tool_result` with that ID or re-run the tool / re-read the file. Never rely on memory of compacted output.
- This conversation will be compacted again when the context limit is reached. Persist important state to files rather than keeping it only in the context window.
"""

ESCALATION_RATIO = 0.95
"""Token estimate must fall under this fraction of the threshold to count as progress."""

@dataclass(frozen=True)
class SummaryCompactResult:
    """Outcome of `Conversation.compact`. `reclaimed_fraction` is only meaningful when compacted."""

    class Status(Enum):
        COMPACTED = auto()
        NOTHING_TO_CONDENSE = auto()
        SUMMARIZE_FAILED = auto()

    status: Status
    message: str
    reclaimed_fraction: float = 0.0

    @property
    def compacted(self) -> bool:
        return self.status is SummaryCompactResult.Status.COMPACTED

@dataclass(frozen=True)
class ToolCallCompactResult:
    """Outcome of `Conversation.compact_toolcall`."""
    reclaimed_count: int
    reclaimed_fraction: float

def compact_conversation(agent: "Agent[Agent.T.Init]", keep_recent: int = 16) -> SummaryCompactResult:
    """
    Condense conversation history via `Conversation.compact`,
    supplying the summarizer and logging.
    """
    from .agent import Agent

    agent.info("Condensing conversation history...")

    def summarize(messages: list[Any]) -> Optional[str]:
        compactor = Agent.inherit(
            agent,
            share_display=False,
            copy_toolbox=False,
            copy_command=False,
        )
        compactor.config.auto_compact.enabled = False
        # summarizer runs headless (NullDisplay cannot prompt), so let the completion
        # retry in _execute_step auto-confirm instead of raising on get_choice
        compactor.config.auto_confirm = True
        compactor.conversation.messages = messages.copy()

        with compactor as ready:
            result = ready.instruct(CONDENSE_PROMPT, _emit_event=False).execute(max_iterations=1)
        if result.is_err():
            error = result.unwrap_err()
            agent.error(f"Failed to condense conversation history: {error.error}")
            return None
        summary = result.unwrap()
        if summary:
            agent.info(f"Conversation history condensed. Summary:\n{summary}")
            return summary
        else:
            # somehow may be ""...
            agent.error("Conversation history condensed but produced an empty summary.")
            return None

    r = agent.conversation.compact(summarize, keep_recent=keep_recent)
    if r.status is SummaryCompactResult.Status.NOTHING_TO_CONDENSE:
        agent.info(r.message)
    return r

class CompactorAbstract(ABC):
    """Token-budget component. Subclasses implement `auto_compact`."""

    def install(self, hooks: "Hooks") -> None:
        hooks.before_execution_step.add(self._on_before_execution_step)

    def _on_before_execution_step(self, args: "HookArgs.BeforeExecutionStepArgs") -> None:
        agent = args.agent
        try:
            self.auto_compact(agent)
        except CancelledError:
            raise
        except Exception as exc:
            # HookRegistry swallows hook exceptions, so log here to keep failures visible
            agent.error(f"Compaction hook failed: {exc}")

    @abstractmethod
    def auto_compact(self, agent: "Agent[Agent.T.Init]") -> None:
        """Called before every model call; compact the conversation if needed.
        Should respect the agent's auto-compaction configuration.
        """
        ...

@dataclass
class AutoCompactor(CompactorAbstract):
    """Cheap tool-call reclaims first, escalating to a full summary when needed."""
    toolcall_keep_max: int = 12
    summary_keep_max: int = 16
    escalation_rounds: int = 2
    """Cheap rounds before escalating to a summary."""
    max_retries: int = 2
    """Further full compactions allowed while the token estimate stays over the threshold."""

    def auto_compact(self, agent: "Agent[Agent.T.Init]") -> None:
        self._auto_compact(agent, self.toolcall_keep_max, self.summary_keep_max, self.max_retries)

    def _auto_compact(
        self, 
        agent: "Agent[Agent.T.Init]", 
        toolcall_keep_max: int, 
        summary_keep_max: int, 
        remaining: int, 
    ) -> None:
        if not (ac := agent.config.auto_compact).enabled:
            return
        conv = agent.conversation
        # `total_tokens` is reset to None by Conversation.compact until the next model call refreshes it
        if conv.total_tokens is None or conv.total_tokens <= ac.token_threshold:
            return

        # Cheap first: shrinking old tool results costs no API call. Escalate to a summary
        # once enough cheap rounds pile up, or when the reclaim did not sufficiently reduce the estimated token count.
        agent.info("Auto-compaction of old tool results...")
        reclaimed = conv.compact_toolcall(keep_max = toolcall_keep_max)
        estimated_token_after_reclaim = int(conv.total_tokens * (1 - reclaimed.reclaimed_fraction))
        agent.info(
            f"Last call used {conv.total_tokens} tokens, over the auto-compact threshold "
            f"({ac.token_threshold}); compacted {reclaimed.reclaimed_count} tool calls, estimated tokens after reclaim: {estimated_token_after_reclaim:.0f}"
        )

        if (
            conv.compaction_counter.tool_rounds < self.escalation_rounds
            and estimated_token_after_reclaim <= ac.token_threshold * ESCALATION_RATIO
        ):
            return

        agent.info("Escalating to full conversation compaction...")
        try:
            r = compact_conversation(agent, keep_recent = summary_keep_max)
            if not r.compacted:
                return  # compact_conversation already reported why
            if (et:=int(estimated_token_after_reclaim * (1 - r.reclaimed_fraction))) <= ac.token_threshold * ESCALATION_RATIO:
                return
            if remaining == 0:
                agent.error(f"Conversation still ~{et} tokens after compaction; no further progress possible.")
            else:
                agent.warning(f"Estimated tokens after full conversation compaction: {et}. Will auto-compact again.")
                conv.total_tokens = et      # must update before recursive auto-compaction
                self._auto_compact(agent, toolcall_keep_max//2, summary_keep_max//2, remaining - 1)
        except CancelledError:
            raise
        except Exception as exc:
            agent.error(f"Auto-compaction failed: {exc}")
