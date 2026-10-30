"""Parse raw input into instructions and run them against an agent.

The dispatch here is the single execution path shared by the CLI loop, the
non-interactive entry, and (per message/command) the web display; the
`retry` continuation lives only in `run_command`.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel

from ..core.types import CancelledError, Result

if TYPE_CHECKING:
    from ..core.agent import Agent

IMAGE_PREFIX = "image:"


class MessageInstruction(BaseModel):
    content: str
    images: list[str] = []


class CommandInstruction(BaseModel):
    command: str
    args: Optional[str] = None


Instruction = MessageInstruction | CommandInstruction


def _parse_image_block(block: str) -> list[str] | None:
    images: list[str] = []
    try:
        tokens = shlex.split(block)
    except ValueError:
        return None
    for token in tokens:
        if token.startswith(IMAGE_PREFIX) and len(token) > len(IMAGE_PREFIX):
            images.append(token[len(IMAGE_PREFIX):])
        else:
            return None
    return images or None


def parse_message_input(raw_input: str) -> MessageInstruction:
    content = raw_input.strip()
    if not content.startswith("["):
        return MessageInstruction(content=raw_input)
    block_end = content.find("]")
    if block_end < 0:
        raise ValueError("Invalid input syntax: missing closing ']'.")
    images = _parse_image_block(content[1:block_end].strip())
    if images is None:
        return MessageInstruction(content=raw_input)
    return MessageInstruction(content=content[block_end + 1:].strip(), images=images)


def input_to_instruction(raw_input: str) -> Instruction:
    if raw_input.startswith("/"):
        raw_command = raw_input[1:].strip()
        parts = raw_command.split(maxsplit=1)
        command = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else None
        return CommandInstruction(command=command, args=args)
    if raw_input.startswith("\\/"):
        raw_input = raw_input[1:]
    return parse_message_input(raw_input)


def _report(agent: "Agent[Agent.T.Init]", kind: str, result: "Result") -> None:
    if result.is_err():
        agent.display_error(f"Error executing {kind}: {result.unwrap_err().error}")


def run_message(agent: "Agent[Agent.T.Init]", content: str, images: list[str]) -> None:
    try:
        result = agent.instruct(content, images=images or None).execute()
    except CancelledError:
        raise
    except Exception as e:
        agent.display_error(f"Error executing instruction: {e}")
        return
    _report(agent, "instruction", result)


def run_command(agent: "Agent[Agent.T.Init]", name: str, arguments: Optional[str]) -> None:
    result = agent.execute_command(name, arguments)
    _report(agent, "command", result)
    # a command may ask for a follow-up execution by returning True (e.g. `retry`);
    # `continue` runs it inside its own handler and returns None
    if result.is_ok() and result.unwrap() is True:
        _report(agent, "instruction", agent.execute())


def execute_instruction(agent: "Agent[Agent.T.Init]", inst: Instruction) -> None:
    match inst:
        case CommandInstruction(command=command, args=args):
            run_command(agent, command, args)
        case MessageInstruction(content=content, images=images):
            run_message(agent, content, images)
        case _:
            agent.display_error(f"Invalid instruction: {inst}")
