import argparse, shlex, sys, hashlib, secrets
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Callable, Literal, overload
import docker
from docker.errors import NotFound

from .core.display_abstract import DisplayAbstract
from .core.config import get_home_dir, HOME_COPY_INCLUDE, load_env
from .core.display_store import EventStore
from .displays.display import Display
from .displays.web_display import WebDisplay
from .displays.web_service import SimpleSession, WebDisplayService, WebSession
from .core.toolbox import ToolBox
from .core.agent import Agent
from .core.prompt import get_system_prompt
from .core.command import Command
from .core.types import CancelledError
from .core.workspace import Workspace
from .session import (AgentStore, Instruction, DurableSession, execute_instruction,
                     input_to_instruction, parse_message_input, register_default_commands)
from .tools.common import default_tool_commands
from .supervisor.runtime import copy_directory, start_attached
from .core.util import CONTAINER_ENV_PATTERNS, resolve_environment, parse_env_option


def get_instruction(
    display: DisplayAbstract | None = None,
    agent: "Agent[Agent.T.Init] | None" = None,
) -> Instruction:
    while True:
        raw_input = (display.input(agent=agent) if isinstance(display, Display) else input(">>> ")).strip()
        if raw_input:
            return input_to_instruction(raw_input)

@overload
def setup_agent(
    name: str = "agent",
    tools: list[Callable] = [],
    default_tools: bool = False,
    default_system_prompt: bool = True,
    default_commands: bool = True,
    display: DisplayAbstract | None = None,
    workdir: Path | str | None = None,
    *,
    init: Literal[True] = True,
    ) -> "Agent[Agent.T.Init]": ...
@overload
def setup_agent(
    name: str = "agent",
    tools: list[Callable] = [],
    default_tools: bool = False,
    default_system_prompt: bool = True,
    default_commands: bool = True,
    display: DisplayAbstract | None = None,
    workdir: Path | str | None = None,
    *,
    init: Literal[False],
    ) -> "Agent[Agent.T.Uninit]": ...
def setup_agent(
    name: str = "agent",
    tools: list[Callable] = [],
    default_tools: bool = False,    # off to keep `tools` explicit
    default_system_prompt: bool = True,
    default_commands: bool = True,
    display: DisplayAbstract | None = None,
    workdir: Path | str | None = None,
    *,
    init: bool = True,
    ) -> "Agent[Agent.T.Any]":
    """Quickly set up a CLI agent with builtins; `init=False` returns it
    uninitialized for a session to adopt."""
    toolbox = ToolBox()
    if default_tools:
        # top-agent can spawn worker agents to execute tasks.
        toolbox.with_defaults().with_subagent_provider()
    if tools:
        toolbox.register(*tools)
    agent = Agent(
        name=name, 
        toolbox=toolbox, 
        display=display or Display(),
        workspace=Workspace(workdir=(Path(workdir) if workdir else Path.cwd())),
        )
    if default_system_prompt:
        agent.system(get_system_prompt())
    if default_commands:
        register_default_commands(agent.command, fs_extra=True)
        if default_tools:
            agent.command.register(*default_tool_commands())
    # initialize last: after_initialize hooks observe the fully configured agent
    return agent.initialize() if init else agent


def interactive_session(agent: "Agent[Agent.T.Init]", task = ""):
    if isinstance(agent.display, Display):
        agent.display.set_command_provider(
            lambda: (
                (("help", "Show available commands"),)
                + tuple((command.name, command.description) for command in agent.command.commands.values())
            )
        )
        agent.display.console.print("[dim]Type a message  / commands  @ files  Up history[/dim]")
    if task:
        inst = input_to_instruction(task)
    else:
        inst = get_instruction(agent.display, agent=agent)

    while True:
        try:
            execute_instruction(agent, inst)
        except (KeyboardInterrupt, CancelledError) as e:
            # remove last message if from user, to allow retry
            agent.conversation.pop_last_message_if_user()
            # CancelledError reason is already reported by Agent.execute
            if isinstance(e, KeyboardInterrupt):
                agent.display_error("Execution interrupted by user.")
        inst = get_instruction(agent.display, agent=agent)


def _web_display_factory(store: EventStore | None = None) -> WebDisplay:
    return WebDisplay(expose_files=True, event_store=store)


def _durable_session(
    events: EventStore, stores: list[AgentStore] = []
) -> tuple[WebDisplay, list["Agent[Agent.T.Uninit]"]]:
    """One agent per store (loaded from its snapshot), or one fresh when none are stored."""
    display = _web_display_factory(events)
    agents = []
    for store in stores or [None]:
        agent = setup_agent(default_tools=True, default_commands=True, display=display, init=False)
        if store is not None:
            store.load(agent)
        agents.append(agent)
    return display, agents


_durable_new = partial(DurableSession.new, factory=_durable_session)
_durable_discover = partial(DurableSession.discover, factory=_durable_session)
_durable_fork = partial(DurableSession.fork, factory=_durable_session)


def _fork_session(source: WebSession) -> WebSession:
    """Fork a mounted session: a complete copy (files, agents, history) plus a
    developer notice steering agents away from the source's paths."""
    if not isinstance(source, DurableSession):
        raise ValueError("Only durable sessions can be forked")
    forked = _durable_fork(source.session_id)
    workdir = forked.directory / "workspace"
    for agent in forked.display.agents.values():
        agent.conversation.add_developer_message(
            f"This session was forked from another session; its workspace was copied "
            f"to {workdir}, which is now the working directory. Paths under "
            f"{source.directory} appear in earlier messages but belong to the source "
            "session, which may still live: never read or write them. Use paths "
            "relative to the working directory from now on. "
            "Also use your new temporary directories for any transient files."
        )
    return forked


def _new_session(workdir: Path | None) -> WebSession:
    """One service session: ephemeral under a fixed workdir, durable otherwise."""
    if workdir is not None:
        display = _web_display_factory()
        agent = setup_agent(default_tools=True, default_commands=True, display=display, workdir=workdir)
        return SimpleSession(f"/{secrets.token_hex(6)}", display, release=agent.finalize)
    return _durable_new()


def web_session(
    workdir: Path | str | None = None,
    *,
    host: str = "localhost",
    port: int = 18960,
    token: str = "",
    base_path: str = "",
    manage_sessions: bool = True,
    initial_agent: bool = True,
) -> None:
    """Run a web service with an optional initial agent and dynamic sessions.

    Without a fixed workdir sessions persist under `<home>/sessions/` and are
    remounted on start; a fresh session is created only when none are stored.
    With a fixed workdir, sessions are ephemeral.
    """
    fixed_workdir = Path(workdir) if workdir is not None else None
    service = WebDisplayService(
        host=host,
        port=port,
        token=token,
        base_path=base_path,
        session_factory=(lambda: _new_session(fixed_workdir)) if manage_sessions else None,
        session_forker=_fork_session if manage_sessions and fixed_workdir is None else None,
    )
    sessions: list[WebSession] = []
    try:
        if initial_agent:
            if fixed_workdir is not None:
                sessions.append(_new_session(fixed_workdir))
            else:  # stored sessions return under their old paths; fresh only when none
                sessions.extend(_durable_discover() or [_durable_new()])
        else:
            sessions.append(SimpleSession("/", _web_display_factory()))
        for session in sessions:
            service.mount(session)
        service.start(blocking=True)
    finally:
        service.stop()
        for session in sessions:
            session.close()


def non_interactive_session(agent: "Agent[Agent.T.Init]", instruction: str):
    execute_instruction(agent, input_to_instruction(instruction))


def cli_commands() -> list[Command]:
    def _long_handler(agent: "Agent[Agent.T.Init]", args: list[str]) -> None:
        eol = args[0] if args else "."
        print(f"Multi-line input mode (end with a line containing only {eol!r}):")
        lines: list[str] = []
        while True:
            line = agent.display.input("... ", agent=agent) if isinstance(agent.display, Display) else input("... ")
            if line == eol:
                break
            lines.append(line)
        text = "\n".join(lines)
        if text.strip():
            execute_instruction(agent, parse_message_input(text))

    return [
        Command(
            name="long",
            description="Enter multi-line input mode. Optionally specify an end-of-line marker (default is '.').",
            handler=_long_handler
        ),
        Command(
            name="exit",
            description="Exit the agent.",
            handler=lambda _: sys.exit(0)
        ),
    ]

def main():
    load_env()

    parser = argparse.ArgumentParser(description="Run the agent.")
    parser.add_argument("instruction", type=str, help="The instruction for the agent.", default="", nargs="?")
    parser.add_argument("--non-interactive", action="store_true", help="Run in non-interactive mode (default: interactive).")

    args = parser.parse_args()

    user_input = args.instruction.strip()

    # setup_agent() installs the console Display by default
    agent = setup_agent(
        default_tools=True, 
        default_commands=True, 
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
    finally:
        if Agent.is_initialized(agent):
            agent.finalize()

def main_serve():
    load_env()

    parser = argparse.ArgumentParser(description="Run the agent in web mode.")
    parser.add_argument("workdir", type=str, help="Workspace for all sessions (default: a temporary workspace).", nargs="?", default=None)
    parser.add_argument("--host", type=str, default="localhost", help="Host for the web server (default: localhost).")
    parser.add_argument("--port", type=int, default=18960, help="Port for the web server (default: 18960).")
    parser.add_argument("--token", type=str, default=None, help="Token for accessing the web interface (default: random token).")
    parser.add_argument("--base-path", type=str, default="", help="URL prefix for the web service (default: root).")
    parser.add_argument("--manage-sessions", action=argparse.BooleanOptionalAction, default=True, help="Allow sessions to be created and removed from the web interface (default: enabled).")
    parser.add_argument("--initial-agent", action=argparse.BooleanOptionalAction, default=True, help="Create an initial agent at startup (default: enabled).")
    args = parser.parse_args()
    
    web_session(
        workdir=args.workdir or None,
        host=args.host,
        port=args.port,
        token=args.token or "",
        base_path=args.base_path,
        manage_sessions=args.manage_sessions,
        initial_agent=args.initial_agent,
    )

def main_container():
    load_env()

    parser = argparse.ArgumentParser(description="Run the container")
    parser.add_argument("mount", type=str, help="Directory to mount as /workspace in the container (default: no host mount).", default="", nargs="?")
    parser.add_argument("--copy", action="store_true", help="Copy the mount directory into /workspace instead of bind mounting it.")
    parser.add_argument("--image", type=str, help="Docker image to use for the container.", default="xun")
    parser.add_argument("--env", type=str, help="Pass into the container, comma-separated: NAME=VALUE sets it directly, otherwise it's forwarded from the host by wildcard pattern. XUN_*/_XUN_* are always forwarded.", default=[], nargs="+")
    parser.add_argument("--name", type=str, help="Name of the container.", default=None)
    parser.add_argument("--network", type=str, choices=["bridge", "host"], default="bridge", help="Docker network mode. bridge (default) publishes --port ports; host shares the host network namespace (on macOS this is the Docker VM's, not reachable from the host browser).")
    parser.add_argument("--port", type=str, help="Ports to publish to the host in bridge mode, can be a comma-separated list.", default=["18960"], nargs="+")
    parser.add_argument("--exec", dest="exec_cmd", type=str, help="Command to run in the container (default: xuns; empty string falls back to the image's default CMD).", default=None)
    args = parser.parse_args()

    try:
        env_kw, env_set = parse_env_option(args.env)
    except ValueError as error:
        parser.error(str(error))
    env_kw = CONTAINER_ENV_PATTERNS + ["TERM"] + env_kw
    ports = [p.strip() for pv in args.port for p in pv.split(",") if p.strip()]

    requested_mount = args.mount.strip()
    if args.copy and not requested_mount:
        parser.error("--copy requires a mount directory.")
    mount = str(Path(requested_mount).resolve()) if requested_mount else ""
    envs = resolve_environment(env_kw, env_set, exclude={"XUN_HOME"})    # the image fixes XUN_HOME=/xun
    name: str = args.name if args.name is not None else f"xun-{hashlib.md5((mount or args.image).encode()).hexdigest()[:8]}"
    exec_cmd = args.exec_cmd
    if exec_cmd is None:
        exec_cmd = "xuns . --host 0.0.0.0" if mount else "xuns '' --host '0.0.0.0'"
    client = docker.from_env()
    container = None
    try:
        container = client.containers.create(
            image=args.image,
            name=name,
            command=shlex.split(exec_cmd) if exec_cmd.strip() else None,
            auto_remove=True,
            stdin_open=True,
            tty=True,
            network_mode=args.network,
            volumes={mount: {"bind": "/workspace", "mode": "rw"}} if mount and not args.copy else None,
            working_dir="/workspace" if mount else None,
            ports={f"{port}/tcp": int(port) for port in ports} if args.network == "bridge" else None,
            environment=envs,
        )
        if args.copy:
            copy_directory(mount, container, "/workspace")
        if (home := get_home_dir()).is_dir():
            copy_directory(str(home), container, "/xun", include=HOME_COPY_INCLUDE)    # copy the host home in; start from an empty dir to skip
        start_attached(container, interactive=True)
    except BaseException:
        if container is not None:
            with suppress(NotFound):
                container.remove(force=True)
        raise
    finally:
        client.close()