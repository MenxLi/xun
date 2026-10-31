"""Session layer: the execution plumbing shared by entrypoints and UIs.

Sits between `xun.core` (execution engine) and the presenting layer
(`displays`, CLI loops): instruction parsing and dispatch, default
commands, steering wiring. May import `xun.core` and itself only
(pinned by test/test_layering.py); it never renders or prompts —
errors surface through `agent.display_error`.
"""

from .commands import register_default_commands
from .instruction import (
    CommandInstruction,
    Instruction,
    MessageInstruction,
    execute_instruction,
    input_to_instruction,
    parse_message_input,
    run_command,
    run_message,
)
from .steering import SteeringStore, UserInstruction, install_steering
from .storage import AgentStore, DBEventStore

__all__ = [
    "CommandInstruction",
    "Instruction",
    "MessageInstruction",
    "execute_instruction",
    "input_to_instruction",
    "parse_message_input",
    "run_command",
    "run_message",
    "register_default_commands",
    "SteeringStore",
    "UserInstruction",
    "install_steering",
    "AgentStore",
    "DBEventStore",
]
