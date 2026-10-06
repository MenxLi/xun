"""The agent's data state: ``AgentState`` container and its ``StateEntry`` values."""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Self, overload

from .types import JsonType, ToJson, FromJson


class StateEntry(ToJson, FromJson, ABC):
    """Base class for values stored in ``Agent.state``.

    PERSIST subclasses implement ``to_json`` / ``from_json`` for their payload;
    RUNTIME ones inherit the raising defaults and are skipped by ``AgentState.to_json``.
    """

    class Lifetime(Enum):
        PERSIST = "persist"
        RUNTIME = "runtime"

    lifetime = Lifetime.RUNTIME

    _registry: ClassVar[dict[str, type[StateEntry]]] = {}

    @classmethod
    def _entry_discriminator(cls) -> str:
        return f"{cls.__module__}.{cls.__qualname__}"

    def __init_subclass__(cls, **kwargs: Any):
        super().__init_subclass__(**kwargs)
        cls._registry[cls._entry_discriminator()] = cls

    def to_json(self) -> JsonType:
        raise TypeError(f"'{type(self).__name__}' is not JSON-serializable.")

    @classmethod
    def from_json(cls, data: JsonType) -> Self:
        raise TypeError(f"'{cls.__name__}' is not deserializable.")


@dataclass
class JsonEntry(StateEntry):
    """A JSON value; persisted unless a different lifetime is given."""

    value: JsonType
    lifetime: StateEntry.Lifetime = StateEntry.Lifetime.PERSIST

    def to_json(self) -> JsonType:
        return self.value

    @classmethod
    def from_json(cls, data: JsonType) -> Self:
        return cls(data)


@dataclass
class RuntimeEntry(StateEntry):
    """An arbitrary in-process object, never written to disk."""

    value: Any


@dataclass
class AgentState(ToJson, FromJson):
    """The agent's state container: lifetime-aware entries with dict access and ser/de."""

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
        """Return the entry stored under `key`, creating it with `or_else()` when absent; None if no `or_else`."""
        if (entry := self._entries.get(key)) is not None:
            return entry
        if or_else is None:
            return None
        entry = or_else()
        self._entries[key] = entry
        return entry

    def to_json(self) -> dict[str, JsonType]:
        """Serialize the PERSIST entries; RUNTIME entries are skipped."""
        return {
            key: {"type": entry._entry_discriminator(), "data": entry.to_json()}
            for key, entry in self._entries.items()
            if entry.lifetime is StateEntry.Lifetime.PERSIST
        }

    @classmethod
    def from_json(cls, data: JsonType) -> Self:
        """Rebuild the state from ``to_json`` output."""
        if not isinstance(data, dict):
            raise TypeError(f"AgentState payload must be a JSON object, got {type(data).__name__}")
        entries: dict[str, StateEntry] = {}
        for key, envelope in data.items():
            if not (isinstance(envelope, dict) and isinstance(envelope_type := envelope.get("type"), str)):
                raise ValueError(f"state entry '{key}': not a serialized-state envelope")
            if (entry_cls := StateEntry._registry.get(envelope_type)) is None:
                raise ValueError(f"state entry '{key}': unknown state type '{envelope_type}'")
            entries[key] = entry_cls.from_json(envelope.get("data"))
        return cls(entries)
