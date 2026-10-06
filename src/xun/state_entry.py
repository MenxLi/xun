"""Typed, lifetime-aware entries for ``Agent.state``. """

from __future__ import annotations

from abc import ABC
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Protocol, Self, cast

from .types import JsonType, ToJson, FromJson


class StateEntry(ToJson, FromJson, ABC):
    """Base class for values stored in ``Agent.state``.

    PERSIST subclasses implement ``to_json`` / ``from_json`` for their payload;
    RUNTIME ones inherit the raising defaults and are skipped by ``Agent.dump_state``.
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


class AgentStateProtocol(Protocol):
    state: dict[str, StateEntry]


class AgentStateMixin(AgentStateProtocol):

    def get_state_entry[SE: StateEntry](self, key: str, or_else: Callable[[], SE] | None = None) -> SE:
        """Return the entry stored under `key`, creating it with `or_else()` when absent; KeyError if no `or_else`."""
        if key in self.state:
            return cast(SE, self.state[key])
        if or_else is None:
            raise KeyError(f"no state entry '{key}'")
        entry = or_else()
        self.state[key] = entry
        return entry

    def dump_state(self) -> dict[str, JsonType]:
        """Serialize the PERSIST entries; RUNTIME entries are skipped."""
        return {
            key: {"type": type(entry)._entry_discriminator(), "data": entry.to_json()}
            for key, entry in self.state.items()
            if entry.lifetime is StateEntry.Lifetime.PERSIST
        }

    def load_state(self, data: Mapping[str, JsonType]) -> None:
        """Replace the agent state with entries rebuilt from ``dump_state`` output."""
        loaded: dict[str, StateEntry] = {}
        for key, envelope in data.items():
            if not (isinstance(envelope, dict) and isinstance(envelope_type := envelope.get("type"), str)):
                raise ValueError(f"state entry '{key}': not a serialized-state envelope")
            loaded[key] = StateEntry._registry[envelope_type].from_json(envelope.get("data"))
        self.state.clear()
        self.state.update(loaded)
