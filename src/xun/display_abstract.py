
from typing import TYPE_CHECKING, Generic, TypeVar, Optional
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
if TYPE_CHECKING:
    from .conversation import Conversation

JsonType = str | int | float | bool | None | dict[str, "JsonType"] | list["JsonType"]

@dataclass
class InfoEvent:
    message: str

@dataclass
class ModelWorkingEvent:
    model_call_id: str
    remaining_iterations: Optional[int] = None

@dataclass
class ModelMessageEvent:
    model_call_id: str
    content: str

@dataclass
class ToolCallEvent:
    tool_call_id: str
    tool_name: str
    args: dict[str, JsonType]

@dataclass
class ToolResultEvent:
    tool_call_id: str
    result: JsonType

@dataclass
class ShowHistoryEvent:
    history: list["Conversation.MessageRecord"]

@dataclass 
class ShowHelpEvent:
    message: str

@dataclass
class ErrorEvent:
    message: str

DisplayEventType = (
    ShowHelpEvent
    | ShowHistoryEvent
    | ModelWorkingEvent 
    | ModelMessageEvent 
    | ToolCallEvent 
    | ToolResultEvent
    | InfoEvent
    | ErrorEvent
    )
DisplayEventT = TypeVar( "DisplayEventT", bound=DisplayEventType)
@dataclass
class DisplayEvent(Generic[DisplayEventT]):
    agent_name: Optional[str]
    event: DisplayEventT

@dataclass
class MessageInstruction:
    content: str
    images: list[str] = field(default_factory=list)
@dataclass
class CommandInstruction:
    command: str
    args: list[str] = field(default_factory=list)
Instruction = MessageInstruction | CommandInstruction

def assemble_event(event: DisplayEventT) -> DisplayEvent[DisplayEventT]:
    from .context import execution_context
    if (ctx := execution_context.get()) is not None:
        agent_name = ctx.agent.name
    else:
        agent_name = None
    return DisplayEvent(
        agent_name=agent_name,
        event=event,
    )

class DisplayAbstract(ABC):
    @abstractmethod
    def get_instruction(self) -> Instruction:...

    @abstractmethod
    def get_confirm(
        self,
        prompt: str,
        message: Optional[str] = None, 
        title: Optional[str] = None,
        subtitle: str | None = None,
        default: bool = True, 
        ) -> bool:...

    def emit(self, ev: DisplayEventType):
        event = assemble_event(ev)
        self.on_event(event)

    @abstractmethod
    def on_event(self, event: DisplayEvent):...
