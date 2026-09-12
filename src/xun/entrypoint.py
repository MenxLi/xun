# import for arrow key support in input()
import readline     # noqa

import argparse, shlex, sys, hashlib, tempfile, subprocess, uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, Literal
from pydantic import BaseModel

from .display_abstract import DisplayAbstract
from .displays.display import Display
from .displays.web_display import WebDisplay
from .displays.web_service import WebDisplayService
from .toolbox import ToolBox
from .agent import Agent
from .store import Store
from .prompt import get_system_prompt
from .command import Command
from .types import CancelledError
from .workspace import Workspace
from .tools.common import default_tool_commands

from dotenv import load_dotenv
load_dotenv()


IMAGE_PREFIX = "image:"


class MessageInstruction(BaseModel):
    content: str
    images: list[str] = []

class CommandInstruction(BaseModel):
    command: str
    args: Optional[str] = None

Instruction = MessageInstruction | CommandInstruction


def _parse_image_block(image_block: str) -> list[str] | None:
    images = []
    for token in shlex.split(image_block):
        if not token.startswith(IMAGE_PREFIX) or len(token) <= len(IMAGE_PREFIX):
            return None
        images.append(token[len(IMAGE_PREFIX):])
    return images or None


def _parse_message_input(raw_input: str) -> MessageInstruction:
    content = raw_input.strip()
    if not content.startswith("["):
        return MessageInstruction(content=raw_input)
    image_block_end = content.find("]")
    if image_block_end < 0:
        raise ValueError("Invalid image syntax: missing closing ']'.")
    image_block = content[1:image_block_end].strip()
    images = _parse_image_block(image_block)
    if images is None:
        return MessageInstruction(content=raw_input)
    return MessageInstruction(content=content[image_block_end + 1:].strip(), images=images)


def input_to_instruction(raw_input: str) -> Instruction:
    if raw_input.startswith("/"):
        raw_command = raw_input[1:].strip()
        parts = raw_command.split(maxsplit=1)
        command = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else None
        return CommandInstruction(command=command, args=args)
    if raw_input.startswith("\\/"):
        raw_input = raw_input[1:]
    return _parse_message_input(raw_input)


def get_instruction() -> Instruction:
    while True:
        print("Input (`/help` for help).")
        raw_input = input(">>> ").strip()
        if raw_input:
            return input_to_instruction(raw_input)

def setup_agent(
    name: str = "agent",
    tools: list[Callable] = [],
    default_tools: bool = False,
    default_system_prompt: bool = True,
    default_commands: bool = True,
    persistent_store: Path | None = None,
    display: DisplayAbstract | None = None,
    workdir: Path | str | None = None,
    ) -> "Agent[Agent.T.Init]":
    toolbox = ToolBox()
    if default_tools:
        # top-agent can spawn worker agents to execute tasks.
        toolbox.with_defaults().with_subagent_provider()
    if tools:
        toolbox.register(*tools)
    agent = Agent(
        name=name, 
        toolbox=toolbox, 
        persistent_store=persistent_store, 
        display=display or Display(),
        workspace=Workspace(workdir=(Path(workdir) if workdir else Path.cwd())),
        )
    if default_system_prompt:
        agent.system(get_system_prompt())
    if default_commands:
        agent.command.with_defaults()
        if default_tools:
            agent.command.register(*default_tool_commands())
    # initialize last: after_initialize hooks observe the fully configured agent
    return agent.initialize()


def _execute_instruction(inst: Instruction, agent: "Agent[Agent.T.Init]"):
    match inst:
        case CommandInstruction():
            agent.execute_command(inst.command, inst.args)
            if inst.command == "retry":
                agent.execute()

        case MessageInstruction():
            try:
                agent.instruct(inst.content, images=inst.images).execute()
            except ValueError as e:
                agent.error(f"Error executing instruction: {e}")
        case _:
            agent.error(f"Invalid instruction: {inst}")

def interactive_session(agent: "Agent[Agent.T.Init]", task = ""):
    if task:
        inst = input_to_instruction(task)
    else:
        inst = get_instruction()

    while True:
        try:
            _execute_instruction(inst, agent)
        except (KeyboardInterrupt, CancelledError):
            # remove last message if from user, to allow retry
            agent.conversation.pop_last_message_if_user()
            agent.error("Execution interrupted by user.")
        inst = get_instruction()


@contextmanager
def _web_display_session(
    workdir: Path | None,
    *,
    mount_path: str,
    persistent_store: Path | None,
) -> Iterator[tuple[str, WebDisplay]]:
    temporary_workspace = tempfile.TemporaryDirectory(suffix="-workspace") if workdir is None else None
    session_workdir = Path(temporary_workspace.name) if temporary_workspace else workdir
    try:
        display = WebDisplay(expose_files=True)
        agent = setup_agent(
            name=f"agent-{hashlib.md5(str(session_workdir).encode()).hexdigest()[:8]}",
            persistent_store=persistent_store,
            default_tools=True,
            default_commands=True,
            display=display,
            workdir=session_workdir,
        )
        try:
            yield mount_path, display
        finally:
            agent.finalize()
    finally:
        if temporary_workspace is not None:
            temporary_workspace.cleanup()


def web_session(
    workdir: Path | str | None = None,
    *,
    host: str = "localhost",
    port: int = 18960,
    token: str = "",
    base_path: str = "",
    persistent_store: Path | None = None,
    manage_sessions: bool = True,
) -> None:
    """Run a web service with one initial agent and optional dynamic sessions."""
    fixed_workdir = Path(workdir) if workdir is not None else None

    def new_session():
        mount_path = f"/{uuid.uuid4()}"
        return _web_display_session(
            fixed_workdir,
            mount_path=mount_path,
            persistent_store=persistent_store,
        )

    with _web_display_session(
        fixed_workdir,
        mount_path="/",
        persistent_store=persistent_store,
    ) as (mount_path, display):
        service = WebDisplayService(
            host=host,
            port=port,
            token=token,
            base_path=base_path,
            session_manager=new_session if manage_sessions else None,
        ).mount(mount_path, display)
        try:
            service.start(blocking=True)
        finally:
            service.stop()


def non_interactive_session(agent: "Agent[Agent.T.Init]", instruction: str):
    inst = input_to_instruction(instruction)
    _execute_instruction(inst, agent)

def cli_commands() -> list[Command]:
    def _long_handler(agent: "Agent[Agent.T.Init]", args: list[str]) -> None:
        eol = args[0] if args else "."
        print(f"Multi-line input mode (end with a line containing only {eol!r}):")
        lines: list[str] = []
        while True:
            line = input("... ")
            if line == eol:
                break
            lines.append(line)
        text = "\n".join(lines)
        if text.strip():
            inst = _parse_message_input(text)
            _execute_instruction(inst, agent)

    def _render_handler(agent: "Agent[Agent.T.Init]", arguments: list[str]) -> None:
        if not arguments:
            agent.error("Please provide a file path to save the rendered HTML.")
            return
        html = agent.conversation.render_history_as_html()
        aim_path = Path(arguments[0])
        aim_path.write_text(html, encoding="utf-8")
    
    return [
        Command(
            name="long",
            description="Enter multi-line input mode. Optionally specify an end-of-line marker (default is '.').",
            handler=_long_handler
        ),
        Command(
            name="render",
            description="Render the conversation history as HTML, output to the specified file path.",
            handler=_render_handler
        ),
        Command(
            name="exit",
            description="Exit the agent.",
            handler=lambda _: sys.exit(0)
        ),
    ]

def main():

    parser = argparse.ArgumentParser(description="Run the agent.")
    parser.add_argument("instruction", type=str, help="The instruction for the agent.", default="", nargs="?")
    parser.add_argument("--persist", action="store_true", help="Whether to track the agent's conversation history in the default store.")
    parser.add_argument("--non-interactive", action="store_true", help="Run in non-interactive mode (default: interactive).")

    args = parser.parse_args()

    user_input = args.instruction.strip()

    if args.persist:
        store = Store()
        persistent_store = store.running_agent_store
    else:
        persistent_store = None

    agent = setup_agent(
        persistent_store=persistent_store, 
        default_tools=True, 
        default_commands=True, 
        display=Display()
        )

    agent.command.register(*cli_commands())
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        if is_tty and not args.non_interactive:
            interactive_session(agent, user_input)
        else:
            if not user_input:
                raise ValueError("Instruction is required in non-interactive mode.")
            non_interactive_session(agent, user_input)
    except:
        raise
    finally:
        if Agent.is_initialized(agent):
            agent.finalize()

def main_serve():
    parser = argparse.ArgumentParser(description="Run the agent in web mode.")
    parser.add_argument("workdir", type=str, help="Workspace for all sessions (default: a temporary workspace).", nargs="?", default=None)
    parser.add_argument("--host", type=str, default="localhost", help="Host for the web server (default: localhost).")
    parser.add_argument("--port", type=int, default=18960, help="Port for the web server (default: 18960).")
    parser.add_argument("--token", type=str, default=None, help="Token for accessing the web interface (default: random token).")
    parser.add_argument("--base-path", type=str, default="", help="URL prefix for the web service (default: root).")
    parser.add_argument("--persist", action="store_true", help="Whether to track the agent's conversation history in the default store.")
    parser.add_argument("--manage-sessions", action=argparse.BooleanOptionalAction, default=True, help="Allow sessions to be created and removed from the web interface (default: enabled).")
    args = parser.parse_args()

    if args.persist:
        store = Store()
        persistent_store = store.running_agent_store
    else:
        persistent_store = None
    
    web_session(
        workdir=args.workdir or None,
        host=args.host,
        port=args.port,
        token=args.token or "",
        base_path=args.base_path,
        persistent_store=persistent_store,
        manage_sessions=args.manage_sessions,
    )

def main_container():
    import os
    import fnmatch

    parser = argparse.ArgumentParser(description="Run the container")
    parser.add_argument("mount", type=str, help="Directory to mount as /workspace in the container (default: no host mount).", default="", nargs="?")
    parser.add_argument("--copy", action="store_true", help="Copy the mount directory into /workspace instead of bind mounting it.")
    parser.add_argument("--image", type=str, help="Docker image to use for the container.", default="xun")
    parser.add_argument("--env", type=str, help="Environment variables to pass into the container, can be a comma-separated wildcard list. Will always include XUN_*/_XUN_* by default.", default=[], nargs="+")
    parser.add_argument("--name", type=str, help="Name of the container.", default=None)
    parser.add_argument("--network", type=str, choices=["bridge", "host"], default="bridge", help="Docker network mode. bridge (default) publishes --port ports; host shares the host network namespace (on macOS this is the Docker VM's, not reachable from the host browser).")
    parser.add_argument("--port", type=str, help="Ports to publish to the host in bridge mode, can be a comma-separated list.", default=["18960"], nargs="+")
    parser.add_argument("--exec", dest="exec_cmd", type=str, help="Command to run in the container (default: xuns; empty string falls back to the image's default CMD).", default=None)
    args = parser.parse_args()

    env_kw = ["XUN_*", "_XUN_*"] + [e.strip() for ev in args.env for e in ev.split(",")]
    ports = [p.strip() for pv in args.port for p in pv.split(",") if p.strip()]

    requested_mount = args.mount.strip()
    if args.copy and not requested_mount:
        parser.error("--copy requires a mount directory.")
    mount = str(Path(requested_mount).resolve()) if requested_mount else ""
    envs: dict = {k: v for k, v in os.environ.items() if any(fnmatch.fnmatch(k, pattern) for pattern in env_kw)}
    name: str = args.name if args.name is not None else f"xun-{hashlib.md5((mount or args.image).encode()).hexdigest()[:8]}"
    network: Literal['bridge', 'host'] = args.network

    cmd = [
        "docker", "create",
        "--rm",
        "-it",
        "--name", name,
        "--network", network,
    ]
    if mount:
        if not args.copy:
            cmd += ["--volume", f"{mount}:/workspace"]
        cmd += ["--workdir", "/workspace"]
    if network == "bridge":
        for port in ports:
            cmd += ["--publish", f"{port}:{port}"]
    for key, value in envs.items():
        cmd += ["--env", f"{key}={value}"]
    cmd.append(args.image)
    exec_cmd = args.exec_cmd
    if exec_cmd is None:
        exec_cmd = "xuns --host 0.0.0.0" if mount else "xuns '' --host '0.0.0.0'"
    if exec_cmd.strip():
        cmd += shlex.split(exec_cmd)

    subprocess.run(cmd, check=True)
    try:
        if args.copy:
            subprocess.run(["docker", "cp", f"{mount}/.", f"{name}:/workspace"], check=True)
        subprocess.run(["docker", "start", "--attach", "--interactive", name], check=True)
    except BaseException:
        subprocess.run(["docker", "rm", "--force", name], check=False)
        raise