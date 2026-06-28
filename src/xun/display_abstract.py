
from typing import Generic, TypeVar, Optional
from abc import ABC, abstractmethod
from pydantic import BaseModel
from .conversation import Conversation

# https://pydantic.dev/docs/validation/latest/concepts/types/#named-recursive-types
import sys
if sys.version_info >= (3, 12):
    type JsonType = str | int | float | bool | None | dict[str, JsonType] | list[JsonType]
else:
    from typing import Union
    from typing_extensions import TypeAliasType
    JsonType = TypeAliasType(
        'JsonType',
        'Union[dict[str, JsonType], list[JsonType], str, int, float, bool, None]',  
    )


class InfoEvent(BaseModel):
    message: str

class ModelWorkingEvent(BaseModel):
    model_call_id: str
    remaining_iterations: Optional[int] = None

class ModelMessageEvent(BaseModel):
    model_call_id: str
    content: str

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
    message: str

class ErrorEvent(BaseModel):
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

class DisplayEvent(BaseModel, Generic[DisplayEventT]):
    agent_name: Optional[str]
    event: DisplayEventT

class MessageInstruction(BaseModel):
    content: str
    images: list[str] = []
class CommandInstruction(BaseModel):
    command: str
    args: list[str] = []
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
