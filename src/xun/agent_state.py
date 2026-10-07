"""The agent's data state: ``AgentState`` container and its ``StateEntry`` values."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self, overload

from .types import JsonType, ToJson, FromJson


class _StateEntryBase(ABC):
    """Shared registry: every concrete entry class registers under its qualname."""

    _registry: ClassVar[dict[str, type[_StateEntryBase]]] = {}

    @classmethod
    def _entry_discriminator(cls) -> str:
        return f"{cls.__module__}.{cls.__qualname__}"

    def __init_subclass__(cls, **kwargs: Any):
        super().__init_subclass__(**kwargs)
        cls._registry[cls._entry_discriminator()] = cls


class StatePersistEntry(_StateEntryBase, ToJson, FromJson, ABC):
    """A state value with a JSON representation; written to disk by ``AgentState``."""

    @abstractmethod
    def to_json(self) -> JsonType: ...

    @classmethod
    @abstractmethod
    def from_json(cls, data: JsonType) -> Self: ...


class StateRuntimeEntry(_StateEntryBase, ABC):
    """A state value that only exists in-process and is never serialized."""


type StateEntry = StatePersistEntry | StateRuntimeEntry


@dataclass
class JsonEntry(StatePersistEntry):
    """An arbitrary JSON value kept in the state."""

    value: JsonType

    def to_json(self) -> JsonType:
        return self.value

    @classmethod
    def from_json(cls, data: JsonType) -> Self:
        return cls(data)


@dataclass
class RuntimeEntry[T](StateRuntimeEntry):
    """An arbitrary in-process object."""

    value: T


@dataclass
class AgentState(ToJson, FromJson):
    """The agent's state container: persist/runtime entries with dict access and ser/de."""

    _entries: dict[str, StateEntry] = field(default_factory=dict)

    def __getitem__(self, key: str) -> StateEntry:
        return self._entries[key]

    def __setitem__(self, key: str, entry: StateEntry):
        self._entries[key] = entry

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    @overload
    def get_entry[SE: StateEntry](self, key: str, or_else: Callable[[], SE]) -> SE: ...
    @overload
    def get_entry(self, key: str, or_else: None = None) -> StateEntry | None: ...
    def get_entry(self, key: str, or_else: Callable[[], StateEntry] | None = None) -> StateEntry | None:
        if (entry := self._entries.get(key)) is not None:
            return entry
        if or_else is None:
            return None
        entry = or_else()
        self._entries[key] = entry
        return entry

    def to_json(self) -> dict[str, JsonType]:
        """Serialize the persist entries; runtime entries are skipped."""
        return {
            key: {"type": entry._entry_discriminator(), "data": entry.to_json()}
            for key, entry in self._entries.items()
            if isinstance(entry, StatePersistEntry)
        }

    @classmethod
    def from_json(cls, data: JsonType) -> Self:
        if not isinstance(data, dict):
            raise TypeError(f"AgentState payload must be a JSON object, got {type(data).__name__}")
        entries: dict[str, StateEntry] = {}
        for key, envelope in data.items():
            if not (isinstance(envelope, dict) and isinstance(envelope_type := envelope.get("type"), str)):
                raise ValueError(f"state entry '{key}': not a serialized-state envelope")
            entry_cls = _StateEntryBase._registry.get(envelope_type)
            if not (isinstance(entry_cls, type) and issubclass(entry_cls, StatePersistEntry)):
                raise ValueError(f"state entry '{key}': unknown persist state type '{envelope_type}'")
            entries[key] = entry_cls.from_json(envelope.get("data"))
        return cls(entries)
