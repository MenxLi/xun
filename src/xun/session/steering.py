"""Steering: user instructions injected while an agent is running.

`SteeringStore` is the thread-safe buffer (drained between execution
steps); `install_steering` installs the consumption policy so every
presenting layer shares it. The store itself is created lazily through
`AgentState.get_entry`, so a plain `Agent` supports steering without
any setup call.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Callable, Sequence

from PIL.Image import Image

from ..core.agent_state import StateRuntimeEntry
from ..core.hooks import HookArgs

if TYPE_CHECKING:
    from ..core.agent import Agent


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


def install_steering(agent: "Agent[Agent.T.Any]", on_consumed: Callable[[Sequence[str]], None] = lambda _keys: None) -> None:
    def flush(args: HookArgs.AfterExecutionStepArgs) -> None:
        items = SteeringStore.of(args.agent).take()
        if not items:
            return
        for _, instruction in items:
            args.agent.instruct(instruction.content, images=instruction.images or None)
        # keep the loop alive so the model sees the injected instructions even on the last step
        args.should_continue = True
        on_consumed([key for key, _ in items])

    agent.hooks.after_execution_step.add(flush)
