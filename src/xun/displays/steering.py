"""Buffer for user instructions injected while an agent is running.

The web display's ``after_execution_step`` flush hook is the sole consumer:
routes only enqueue (while the agent runs); idle input goes through the
normal message path instead.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Sequence

from PIL.Image import Image

from ..agent_state import StateRuntimeEntry

if TYPE_CHECKING:
    from ..agent import Agent

@dataclass
class UserInstruction:
    content: str
    images: Sequence[str | Image]

@dataclass
class SteeringStore(StateRuntimeEntry):
    lock: Lock = field(default_factory=Lock)
    buffer: list[tuple[str, UserInstruction]] = field(default_factory=list)

    @classmethod
    def of(cls, agent: "Agent[Agent.T.Alive]") -> "SteeringStore":
        return agent.state.get_entry("__steering", cls)

    def add(self, value: UserInstruction) -> str:
        key = secrets.token_hex(8)
        with self.lock:
            self.buffer.append((key, value))
        return key

    def swap(self, key: str, value: UserInstruction) -> bool:
        with self.lock:
            for index, (k, _) in enumerate(self.buffer):
                if k == key:
                    self.buffer[index] = (key, value)
                    return True
            return False

    def remove(self, key: str) -> bool:
        with self.lock:
            for index, (k, _) in enumerate(self.buffer):
                if k == key:
                    del self.buffer[index]
                    return True
            return False

    def snapshot(self) -> list[tuple[str, UserInstruction]]:
        with self.lock:
            return list(self.buffer)

    def take(self) -> list[tuple[str, UserInstruction]]:
        with self.lock:
            all_items = self.buffer
            self.buffer = []
            return all_items
