"""Cooperative cancellation: running-state tracking and cancel-event ownership."""

from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event
from typing import cast, Optional, Protocol, TYPE_CHECKING

from .types import CancelledError

if TYPE_CHECKING:
    from .agent import Agent
    from .hooks import HookArgs, Hooks


@dataclass
class LabeledEvent:
    label: str
    event: Event = field(default_factory=Event)


class AgentRunningStateProtocol(Protocol):
    """The Agent surface that running-state helpers rely on."""
    identifier: str
    cancel_event: LabeledEvent
    hooks: "Hooks"
    _running: bool


class AgentRunningStateMixin(AgentRunningStateProtocol):
    """Running-state helpers for Agent: running tracking, cancellation, and the
    run-scoped lifecycle around a unit of work."""

    @property
    def is_running(self) -> bool:
        """True while an execution (model loop or tracked command) is active on this agent."""
        return self._running

    def cancel(self) -> bool:
        """Signal cancellation; returns False if the agent is idle (event left unset)."""
        if not self._running:
            return False
        self.cancel_event.event.set()
        return True

    def check_cancel(self) -> None:
        if self.cancel_event.event.is_set():
            raise CancelledError("Operation cancelled by user.")

    def _clear_cancel(self) -> None:
        """Clear the cancel event if this agent owns it (label matches its identifier)."""
        if self.cancel_event.label == self.identifier:
            self.cancel_event.event.clear()

    @contextmanager
    def cancellable_execution(self):
        """Wrap a unit of running work: track _running (nested-safe), refuse to
        start or finish while cancelled, turn SIGINT into a cancel for worker
        threads, and clear the event on exit.

        On the idle -> running transition (outermost scope only) this fires
        hooks.run_start; hooks.run_end fires exactly when such a
        scope exits, for any reason."""
        from .hooks import HookArgs

        prev_running = self._running
        self._running = True
        scope: Optional[HookArgs.RunArgs] = None
        try:
            self.check_cancel()
            if not prev_running:
                # only fire at real state transitions
                scope = HookArgs.RunArgs(agent=cast("Agent[Agent.T.Init]", self))
                self.hooks.run_start.invoke(scope)
            yield
            self.check_cancel()
        except KeyboardInterrupt:
            # SIGINT hits only this thread; signal worker-thread sub-agents too
            self.cancel()
            raise
        finally:
            self._running = prev_running
            self._clear_cancel()
            if scope is not None:
                self.hooks.run_end.invoke(scope)
