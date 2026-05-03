from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING, Generic, TypeVar
from threading import Lock
import contextvars
from .display import DisplayAbstract
if TYPE_CHECKING:
    from .agent import Agent

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
    tempdir: Path

    @property
    def display(self) -> DisplayAbstract:
        return self.agent.display

execution_context = contextvars.ContextVar[Optional[ExecutionContext]]("execution_context", default=None)

T = TypeVar("T")
class Locked(Generic[T]):
    def __init__(self, value: T):
        self.value = value
        self._lock = Lock()
    def lock(self) -> T:
        with self._lock:
            return self.value
    def set(self, value: T):
        with self._lock:
            self.value = value
@dataclass
class GlobalContext:
    tempdirs: set[Path]
global_context = Locked(
    GlobalContext(
        tempdirs=set(),
        )
    )