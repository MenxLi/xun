"""Display event payloads and the DisplayEvent envelope shared by all displays."""

from __future__ import annotations
from typing import Any, Generic, Optional, TYPE_CHECKING, Sequence, Annotated, Literal, NoReturn, TypeAlias
import json
import re
import time
from html import unescape
from dataclasses import dataclass
from pydantic import BaseModel, Field, PlainSerializer
from pathlib import Path
from PIL.Image import Image
from .command import Command
from .types import JsonType, ModelCapabilityType
from .util import image_to_url
from .conversation import Conversation
from .types import TypeVar
if TYPE_CHECKING:
    from .toolcall import Function
    from .extension import ExtensionInfo


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

class ShowExtensionsEvent(BaseModel):
    """Listing of discovered extensions or skills, shared by both `/extensions` and `/skills`."""
    class ExtensionRecord(BaseModel):
        name: str
        description: str
        status: str
        """`ExtensionStatus` or `SkillStatus` value, rendered as a string."""
        reason: Optional[str] = None
        """Detail for `FAILED` or `SKIPPED` items."""
        source: Optional[str] = None
        """'project' or 'user', for skills."""

    title: str = "Extensions"
    extensions: list[ExtensionRecord] = Field(default_factory=list)

    @classmethod
    def from_infos(cls, infos: Sequence[Any], title: str = "Extensions") -> "ShowExtensionsEvent":
        return cls(title=title, extensions=[
            cls.ExtensionRecord(
                name=info.name,
                description=info.description,
                status=getattr(info.status, "value", info.status),
                reason=info.reason,
                source=getattr(info, "source", None),
            )
            for info in infos
        ])

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
    def from_tools(cls, tools: Sequence["Function"]) -> "ShowToolsEvent":
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

_HTML_BREAK_RE = re.compile(r"<\s*(?:br\s*/?|/(?:p|div|h[1-6]|li|tr|ul|ol|table|section))\s*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]*>", re.DOTALL)

class HTMLInfoEvent(BaseModel):
    """Custom HTML info message; non-HTML displays fall back to `to_text()`. """
    html: str

    title: Optional[str] = None

    def to_text(self) -> str:
        text = _HTML_BREAK_RE.sub("\n", self.html)
        text = _HTML_TAG_RE.sub("", text)
        text = "\n".join(line.strip() for line in unescape(text).splitlines())
        return re.sub(r"\n{3,}", "\n\n", text).strip()

class ConfirmEvent(BaseModel):
    choice: str
    choices: list[str]
    source: Literal["user", "auto"]
    prompt: str
    message: Optional[str] = None

@dataclass(frozen=True)
class ChoiceOutcome[T]:
    """The chosen value from `get_choice` / `get_confirm`, plus its source."""
    choice: T
    source: Literal["user", "auto"]

    def __bool__(self) -> NoReturn:
        # `if agent.get_confirm(...)`, ignoring `.choice`. Fails loudly
        raise TypeError("ChoiceOutcome has no truth value; test `.choice` explicitly")

class WarningEvent(BaseModel):
    message: str

class ErrorEvent(BaseModel):
    message: str

class AgentBindEvent(BaseModel):
    ...

class AgentUnbindEvent(BaseModel):
    ...

class AgentRunningStartEvent(BaseModel):
    ...

class AgentRunningEndEvent(BaseModel):
    ...

DisplayEventType: TypeAlias = (
    ShowHelpEvent
    | ShowToolsEvent
    | ShowHistoryEvent
    | ShowExtensionsEvent
    | AgentBindEvent
    | AgentUnbindEvent
    | AgentRunningStartEvent
    | AgentRunningEndEvent
    | UserCommandEvent
    | UserMessageEvent
    | ModelWorkingEvent
    | ModelMessageEvent
    | ToolCallEvent
    | ToolResultEvent
    | InfoEvent
    | HTMLInfoEvent
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

if TYPE_CHECKING:
    from .display_abstract import AgentDisplayProtocol