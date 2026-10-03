"""Display interface, agent binding, and the agent-side display helpers.

Event payload types live in `display_event`; the event buffer and standalone-HTML
rendering live in `display_store`. Both are re-exported here so existing
`from .display_abstract import ...` (including star imports) keep working.
"""

from __future__ import annotations
from typing import Optional, TYPE_CHECKING, Protocol, Generic, cast
from abc import ABC, abstractmethod
from pydantic import BaseModel
from .workspace import Workspace
from .hooks import HookArgs, Hooks
from .agent_state import T, StateT
from .display_event import *  # re-export event payloads: ModelMessageEvent, ToolCallEvent, ...
from .display_event import (  # names used directly in this module
    AgentInfo, ChoiceOutcome, ConfirmEvent, DisplayEvent, DisplayEventType,
    ErrorEvent, InfoEvent, WarningEvent,
)
from .display_store import DisplayEventStoreMixin
if TYPE_CHECKING:
    from .agent import Agent
    from .config import AgentConfig

class DisplayAbstract(DisplayEventStoreMixin, ABC):
    """
    Display interface, consumed by the framework (Agent / AgentDisplayMixin) only.
    Callers must never invoke these methods directly; always go through the agent's
    display helpers (display_event / info / warning / error / get_choice / get_confirm).
    """
    _agents: dict[str, "Agent[Agent.T.Any]"]

    def bind(self, agent: "Agent[Agent.T.Uninit]") -> None:
        self.agents[agent.identifier] = agent

    def unbind(self, agent: "Agent[Agent.T.Any]") -> None:
        if agent.identifier in self.agents:
            del self.agents[agent.identifier]
    
    @property
    def agents(self) -> dict[str, "Agent[Agent.T.Any]"]:
        """Return the dictionary of {identifier: Agent} for all agents bound to this display."""
        if not hasattr(self, "_agents"):
            self._agents = {}
        return self._agents

    @abstractmethod
    def on_event(self, event: DisplayEvent): ...

    class ChoiceRequest(BaseModel):
        agent_info: AgentInfo
        prompt: str
        choices: list[str]
        message: Optional[str] = None
        title: Optional[str] = None
        subtitle: Optional[str] = None
        default: Optional[str] = None
        allow_extra: bool = False

    @abstractmethod
    def get_choice(self, request: ChoiceRequest) -> str: ...

class AgentDisplayProtocol(Protocol):
    """The Agent surface that display-facing helpers rely on. """
    name: str
    identifier: str
    workspace: Workspace
    display: DisplayAbstract
    hooks: Hooks
    config: AgentConfig

class AgentDisplayMixin(AgentDisplayProtocol, Generic[StateT]):
    """Display-facing helpers for Agent: event emission, messages and prompts.

    Generic over the agent lifecycle state: the hook-firing helpers (info /
    warning / error / get_choice / get_confirm) require the Init state, so they
    are only callable through `Agent[T.Init]` (the self annotations name this
    mixin, not Agent, which keeps them valid supertypes of the class)."""
    def display_event(self, ev: DisplayEventType) -> None:
        event = DisplayEvent(
            name=ev.__class__.__name__,
            agent=AgentInfo.from_agent(self),
            payload=ev,
        )
        self.display._record_event(event)
        self.display.on_event(event)

    def info(self: "AgentDisplayMixin[T.Init]", message: str) -> None:
        args = HookArgs.AgentInfoArgs(agent=cast("Agent[T.Init]", self), message=message)
        self.hooks.before_display_info.invoke(args)
        self.display_event(InfoEvent(message=args.message))

    def warning(self: "AgentDisplayMixin[T.Init]", message: str) -> None:
        args = HookArgs.AgentWarningArgs(agent=cast("Agent[T.Init]", self), message=message)
        self.hooks.before_display_warning.invoke(args)
        self.display_event(WarningEvent(message=args.message))

    def error(self: "AgentDisplayMixin[T.Init]", message: str) -> None:
        args = HookArgs.AgentErrorArgs(agent=cast("Agent[T.Init]", self), message=message)
        self.hooks.before_display_error.invoke(args)
        self.display_event(ErrorEvent(message=args.message))

    def get_choice(
        self: "AgentDisplayMixin[T.Init]",
        prompt: str,
        choices: list[str],
        message: Optional[str] = None,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        default: Optional[str] = None,
        allow_extra: bool = False,
        _skip_auto_confirm: bool = False,
        ) -> ChoiceOutcome[str]:
        """Ask the user to choose, honoring auto-confirm: return the default
        choice without prompting."""
        if self.config.auto_confirm and not _skip_auto_confirm:
            if default in choices:
                choice = default
            elif choices:
                choice = choices[0]
                self.warning(f"No default for prompt {prompt!r}; auto-selected {choice!r}.")
            else:
                raise ValueError(f"No choices available for prompt {prompt!r}")
            self.display_event(ConfirmEvent(prompt=prompt, choices=choices, choice=choice, source="auto", message=message))
            return ChoiceOutcome(choice=choice, source="auto")
        choice = self.display.get_choice(DisplayAbstract.ChoiceRequest(
            agent_info=AgentInfo.from_agent(self),
            prompt=prompt, choices=choices, message=message,
            title=title, subtitle=subtitle, default=default, allow_extra=allow_extra,
        ))
        self.display_event(ConfirmEvent(prompt=prompt, choices=choices, choice=choice, source="user", message=message))
        return ChoiceOutcome(choice=choice, source="user")

    def get_confirm(
        self: "AgentDisplayMixin[T.Init]",
        prompt: str,
        message: Optional[str] = None,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        default: bool = True,
        _skip_auto_confirm: bool = False,
        ) -> ChoiceOutcome[bool]:
        outcome = self.get_choice(
            prompt=prompt,
            choices=["Yes", "No"],
            message=message, title=title, subtitle=subtitle,
            default="Yes" if default else "No",
            _skip_auto_confirm=_skip_auto_confirm,
        )
        return ChoiceOutcome(choice=outcome.choice == "Yes", source=outcome.source)