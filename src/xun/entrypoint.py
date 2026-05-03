# import for arrow key support in input()
import readline     # noqa

import argparse, sys
from dotenv import load_dotenv
from pathlib import Path
from typing import Callable

from .display_abstract import (
    DisplayAbstract, 
    CommandInstruction, MessageInstruction, 
    ErrorEvent, InfoEvent, ShowHelpEvent, ShowHistoryEvent
)
from .display import input_to_instruction
from .context import global_context
from .toolbox import ToolBox
from .agent import Agent
from .store import Store
from .prompt import get_system_prompt


def evaluate_command(instruction: CommandInstruction, agent: Agent):
    display = agent.display
    match instruction.command:
        case "help":
            display.emit(ShowHelpEvent())

        case "restart":
            agent.conversation.clear()
            display.emit(InfoEvent(message="Conversation history cleared."))

        case "retry":
            records = agent.conversation.pop_from_last_user_message()
            assert records and isinstance(records, list) and len(records) > 0 and isinstance(records[0], dict) and records[0].get("role") == "user"
            msg = records[0]["content"]
            display.emit(InfoEvent(message=f"Cleared to last user message. ({msg[:50] + '...' if len(msg) > 50 else msg})"))

        case "revise":
            agent.conversation.pop_from_last_user_message(inclusive=False)
            display.emit(InfoEvent(message="Cleared to last user message."))

        case "config":
            config = agent.app_config
            display.emit(InfoEvent(message=str(config.dict())))

        case "tools":
            tools = agent.toolbox.list_tools()
            if not tools:
                display.emit(InfoEvent(message="No tools registered."))
                return
            display.emit(InfoEvent(message="\n".join([f"{tool.name}: {tool.description}" for tool in tools])))

        case "dump":
            store = Store()
            agent.dump(aim_dir:=store.next_history_store())
            display.emit(InfoEvent(message=f"Conversation history dumped to {aim_dir}"))

        case "load":
            if instruction.args:
                aim_dir = Path(instruction.args[0])
                if not aim_dir.exists():
                    display.emit(ErrorEvent(message=f"File {aim_dir} does not exist."))
                    return
                if not aim_dir.is_dir():
                    display.emit(ErrorEvent(message=f"{aim_dir} is not a directory."))
                    return
            else:
                store = Store()
                latest_dir = store.latest_history_store()
                if latest_dir is None:
                    display.emit(InfoEvent(message="No conversation history found."))
                    return
                aim_dir = latest_dir
            agent.load(aim_dir)
            display.emit(InfoEvent(message=f"Conversation history loaded from {aim_dir}"))

        case "condense":
            agent.condense_conversation()

        case "history":
            display.emit(
                ShowHistoryEvent(history=agent.conversation.to_history())
                )

        case "exit":
            print("Bye!")
            exit(0)

        case _:
            display.emit(ErrorEvent(message=f"Unknown command: {instruction.command}"))

def setup_agent(
    name: str = "agent",
    tools: list[Callable] = [],
    default_tools: bool = True,
    default_system_prompt: bool = True,
    persistent_store: Path | None = None,
    display: DisplayAbstract | None = None,
    ) -> Agent:
    toolbox = ToolBox()
    if default_tools:
        # top-agent can spawn worker agents to execute tasks.
        toolbox.with_defaults().with_subagent_provider()
    if tools:
        toolbox.register_many(tools)
    agent = Agent(
        name=name, 
        toolbox=toolbox, 
        persistent_store=persistent_store, 
        display=display
        )
    if default_system_prompt:
        agent.system(get_system_prompt())
    return agent

def interactive_session(agent: Agent, task = ""):
    display = agent.display
    if task:
        inst = input_to_instruction(task)
    else:
        inst = display.get_instruction()

    while True:
        match inst:
            case CommandInstruction():
                evaluate_command(inst, agent)
            case MessageInstruction():
                agent.instruct(inst.content).execute()
            case _:
                display.emit(ErrorEvent(message=f"Invalid instruction: {inst}"))
        inst = display.get_instruction()

def non_interactive_session(agent: Agent, instruction: str):
    inst = input_to_instruction(instruction)
    match inst:
        case CommandInstruction():
            evaluate_command(inst, agent)
        case MessageInstruction():
            agent.instruct(inst.content).execute()
        case _:
            agent.display.emit(ErrorEvent(message=f"Invalid instruction: {inst}"))

def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Run the agent.")
    parser.add_argument("instruction", type=str, help="The instruction for the agent.", default="", nargs="?")
    parser.add_argument("--persist", action="store_true", help="Whether to track the agent's conversation history in the default store.")
    parser.add_argument("--non-interactive", action="store_true", help="Run in non-interactive mode, the instruction will be executed directly without interactive command loop. ")
    args = parser.parse_args()

    user_input = args.instruction.strip()

    if args.persist:
        store = Store()
        persistent_store = store.running_agent_store
    else:
        persistent_store = None

    agent = setup_agent(persistent_store=persistent_store)
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not args.non_interactive
    if interactive:
        interactive_session(agent, user_input)
    else:
        if not user_input:
            raise ValueError("Instruction is required in non-interactive mode.")
        non_interactive_session(agent, user_input)

__all__ = ["main", "setup_agent", "interactive_session"]
