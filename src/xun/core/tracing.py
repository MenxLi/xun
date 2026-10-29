
from __future__ import annotations
from contextlib import contextmanager
from pydantic import BaseModel, ConfigDict

class SpanSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    parent: SpanSnapshot | None = None
    execution: str | None = None
    step: str | None = None
    tool_call: str | None = None

class SpanContext:
    _parent: SpanSnapshot | None
    _execution: str | None
    _step: str | None
    _tool_call: str | None

    def __init__(self, parent: SpanContext | SpanSnapshot | None = None):
        self.set_parent(parent)
        self._execution = None
        self._step = None
        self._tool_call = None

    def set_parent(self, parent: SpanContext | SpanSnapshot | None):
        if isinstance(parent, SpanContext):
            self._parent = parent.snapshot()
        else:
            self._parent = parent

    @property
    def parent(self) -> SpanSnapshot | None:
        return self._parent

    @property
    def execution(self) -> str | None:
        return self._execution

    @property
    def step(self) -> str | None:
        return self._step

    @property
    def tool_call(self) -> str | None:
        return self._tool_call

    def snapshot(self) -> SpanSnapshot:
        return SpanSnapshot(
            parent=self._parent,
            execution=self._execution,
            step=self._step,
            tool_call=self._tool_call,
        )

    @contextmanager
    def execution_scope(self, identifier: str):
        prev = self._execution
        try:
            self._execution = identifier
            yield self
        finally:
            self._execution = prev

    @contextmanager
    def step_scope(self, identifier: str):
        prev = self._step
        try:
            self._step = identifier
            yield self
        finally:
            self._step = prev

    @contextmanager
    def tool_call_scope(self, identifier: str):
        prev = self._tool_call
        try:
            self._tool_call = identifier
            yield self
        finally:
            self._tool_call = prev
