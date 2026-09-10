from __future__ import annotations
from typing import Generic, Optional, TYPE_CHECKING, Protocol, Sequence, Annotated, Literal
import time
from abc import ABC, abstractmethod
from pydantic import BaseModel, Field, PlainSerializer
from pathlib import Path
from PIL.Image import Image
from .command import Command
from .types import JsonType, ModelCapabilityType
from .util import image_to_url
from .conversation import Conversation
from .types import TypeVar
from .workspace import Workspace
if TYPE_CHECKING:
    from .agent import Agent
    from .config import AgentConfig
    from .toolcall import Function

class ModelWorkingEvent(BaseModel):
    model_call_id: str
    remaining_iterations: Optional[int] = None

class ModelMessageEvent(BaseModel):
    model_call_id: str
    content: str
    reasoning: Optional[str] = None
    total_tokens: int
    """ Total tokens consumed by the conversation so far, as reported by the provider. """

class ToolCallEvent(BaseModel):
    tool_call_id: str
    tool_name: str
    args: dict[str, JsonType]

class ToolResultEvent(BaseModel):
    tool_call_id: str
    result: JsonType

class ShowHistoryEvent(BaseModel):
    history: list[Conversation.MessageRecord]

class ShowHelpEvent(BaseModel):
    class _HelpCommand(BaseModel):
        name: str
        description: str

    commands: list[_HelpCommand] = []

    @classmethod
    def from_commands(cls, cmds: Sequence[Command]) -> "ShowHelpEvent":
        return cls(commands=[
            cls._HelpCommand(name="help", description="Show this help message."),
            *[
            cls._HelpCommand(
                name=cmd.name, 
                description=cmd.description
                ) for cmd in cmds
            ],
            ])

class ShowToolsEvent(BaseModel):
    class ToolInfo(BaseModel):
        name: str
        description: str
        required_capabilities: list[ModelCapabilityType] = Field(default_factory=list)

    tools: list[ToolInfo] = Field(default_factory=list)

    @classmethod
    def from_tools(cls, tools: Sequence[Function]) -> "ShowToolsEvent":
        return cls(tools=[
            cls.ToolInfo(
                name=tool.name,
                description=tool.description,
                required_capabilities=sorted(tool.required_capabilities),
            )
            for tool in tools
        ])

class UserCommandEvent(BaseModel):
    name: str
    arguments: Optional[str] = None

class UserMessageEvent(BaseModel):
    class ImageDescriptor(BaseModel):
        kind: Literal["url", "base64"]
        value: str
    content: str
    images: list[ImageDescriptor] = Field(default_factory=list)

    @classmethod
    def from_inputs(
        cls,
        content: str,
        images: Sequence[str | Image] | None = None,
    ) -> "UserMessageEvent":
        return cls(
            content=content,
            images=[
                cls.ImageDescriptor(
                    kind="base64" if image_url.startswith("data:") else "url",
                    value=image_url,
                )
                for image_url in (image_to_url(image) for image in images or ())
            ],
        )

class InfoEvent(BaseModel):
    message: str

class ConfirmEvent(BaseModel):
    choice: str
    choices: list[str]
    source: Literal["user", "auto"]
    prompt: str
    message: Optional[str] = None

class WarningEvent(BaseModel):
    message: str

class ErrorEvent(BaseModel):
    message: str

class AgentBindEvent(BaseModel):
    ...

class AgentUnbindEvent(BaseModel):
    ...

DisplayEventType = (
    ShowHelpEvent
    | ShowToolsEvent
    | AgentBindEvent
    | AgentUnbindEvent
    | UserCommandEvent
    | UserMessageEvent
    | ShowHistoryEvent
    | ModelWorkingEvent
    | ModelMessageEvent
    | ToolCallEvent
    | ToolResultEvent
    | InfoEvent
    | ConfirmEvent
    | WarningEvent
    | ErrorEvent
)
DisplayEventT = TypeVar("DisplayEventT", bound=DisplayEventType)

def _ser_path(path: Path) -> str:
    return str(path.resolve())

class AgentInfo(BaseModel):
    name: str
    identifier: str
    workdir: Annotated[Path, PlainSerializer(_ser_path)]

    @staticmethod
    def from_agent(agent: "AgentDisplayProtocol") -> AgentInfo:
        return AgentInfo(name=agent.name, identifier=agent.identifier, workdir=agent.workspace.workdir)

class DisplayEvent(BaseModel, Generic[DisplayEventT]):
    timestamp: float = Field(default_factory=lambda: time.time())
    """ The timestamp when the event was created, in seconds since the epoch.  """

    name: str
    """ The name of the event type, e.g. 'ModelMessageEvent', 'ToolCallEvent', etc. """

    agent: AgentInfo
    """ The agent that emitted the event. Represented as an `AgentInfo` object. """

    payload: DisplayEventT
    """ The actual event data. """

    def to_json(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_json(cls, data: dict) -> DisplayEvent[DisplayEventT]:
        event_name = data.get("name")
        if not event_name:
            raise ValueError("Missing 'name' field in DisplayEvent JSON data")
        event_cls = globals().get(event_name)
        if not event_cls or not issubclass(event_cls, BaseModel):
            raise ValueError(f"Unknown event type: {event_name}")
        event_data = data.get("payload", {})
        agent_info = AgentInfo(**data["agent"])
        timestamp = data.get("timestamp", time.time())
        return cls(
            name=event_name,
            agent=agent_info,
            timestamp=timestamp,
            payload=event_cls(**event_data)   # type: ignore
        )

class DisplayAbstract(ABC):
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
        if hasattr(self, "_agents"):
            return self._agents
        else:
            setattr(self, "_agents", {})
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
    config: AgentConfig

class AgentDisplayMixin(AgentDisplayProtocol):
    """Display-facing helpers for Agent: event emission, messages and prompts.  """
    def display_event(self, ev: DisplayEventType) -> None:
        self.display.on_event(DisplayEvent(
            name=ev.__class__.__name__,
            agent=AgentInfo.from_agent(self),
            payload=ev,
        ))

    def info(self, message: str) -> None:
        self.display_event(InfoEvent(message=message))

    def warning(self, message: str) -> None:
        self.display_event(WarningEvent(message=message))

    def error(self, message: str) -> None:
        self.display_event(ErrorEvent(message=message))

    def get_choice(
        self,
        prompt: str,
        choices: list[str],
        message: Optional[str] = None,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        default: Optional[str] = None,
        allow_extra: bool = False,
        _skip_auto_confirm: bool = False,
        ) -> str:
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
            return choice
        choice = self.display.get_choice(DisplayAbstract.ChoiceRequest(
            agent_info=AgentInfo.from_agent(self),
            prompt=prompt, choices=choices, message=message,
            title=title, subtitle=subtitle, default=default, allow_extra=allow_extra,
        ))
        self.display_event(ConfirmEvent(prompt=prompt, choices=choices, choice=choice, source="user", message=message))
        return choice

    def get_confirm(
        self,
        prompt: str,
        message: Optional[str] = None,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        default: bool = True,
        ) -> bool:
        choice = self.get_choice(
            prompt=prompt,
            choices=["Yes", "No"],
            message=message, title=title, subtitle=subtitle,
            default="Yes" if default else "No",
        )
        return choice == "Yes"