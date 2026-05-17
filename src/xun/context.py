from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING, Generic, TypeVar
from threading import Lock
import contextvars
from .display import DisplayAbstract
if TYPE_CHECKING:
    from .agent import Agent, AgentTempDir

@dataclass
class ToolCallContext:
    agent: "Agent"
    tool_name: str

    @property
    def display(self) -> DisplayAbstract:
        return self.agent.display

tool_call_context = contextvars.ContextVar[Optional[ToolCallContext]]("tool_call_context", default=None)

@dataclass
class ExecutionContext:
    agent: "Agent"

    @property
    def display(self) -> DisplayAbstract:
        return self.agent.display

execution_context = contextvars.ContextVar[Optional[ExecutionContext]]("execution_context", default=None)

T = TypeVar("T")
class Guarded(Generic[T]):
    def __init__(self, value: T):
        self.value = value
        self._lock = Lock()
    def __enter__(self):
        self._lock.acquire()
        return self.value
    def __exit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()
@dataclass
class GlobalContext:
    tempdirs: set["AgentTempDir"]
global_context_guard = Guarded(
    GlobalContext(
        tempdirs=set(),
        )
    )