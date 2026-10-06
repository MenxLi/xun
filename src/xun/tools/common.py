from __future__ import annotations
import fnmatch
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Sequence, TYPE_CHECKING
from PIL.Image import Image

from ..hooks import HookArgs
from ..state_entry import StateEntry
from ..toolcall import ToolCallContext as Context
from ..util import image_to_url
from ..workspace import ResolvedPath

if TYPE_CHECKING:
    from ..agent import Agent
    from ..command import Command

def resolve_path(ctx: Context, path: str | Path, raise_on_invalid: bool = True) -> ResolvedPath:
    """ Resolve a path relative to the agent's workspace (workdir / tempdir scope). """
    return ctx.agent.workspace.resolve(path, raise_on_invalid=raise_on_invalid)


def defer_tool_image(ctx: Context, image: str | Image, msg: str = "") -> None:
    """Add an image after the current batch of tool results is committed."""
    image_url = image_to_url(image)

    def add_image(args: HookArgs.AfterExecutionStepArgs) -> None:
        args.agent.conversation.add_user_message(msg, images=[image_url])

    ctx.agent.hooks.after_execution_step.add_once(add_image)


def is_path_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
            return b"\x00" in chunk
    except OSError:
        return True


def glob_match(pattern: str, name: str) -> bool:
    return fnmatch.fnmatch(name, pattern)


def git_ignored_paths(base: Path, paths: list[str]) -> set[str]:
    """
    Return the subset of `paths` that are ignored by git at `base` (a git repo root).
    Paths are given relative to `base`. Uses `git check-ignore --stdin` in one call.
    Returns an empty set if the query fails (e.g. not a git repo), i.e. no filtering.
    """
    if not paths:
        return set()
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=base,
            input="\n".join(str(p) for p in paths) + "\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode not in (0, 1):
        return set()
    return {line for line in result.stdout.splitlines() if line}


class WriteAllowList:
    """
    Track paths that the agent is allowed to write to.
    Entries map a resolved path to whether it was a directory at add time
    (default: file). Directory entries match everything under the path.
    Should be stored in the agent's state, not global.
    """
    def __init__(self):
        self.entries: dict[Path, bool] = {}

    def add(self, path: Path, is_dir: bool = False):
        """
        Add a path (file or directory) to the allowlist.
        Records whether the entry is a directory at add time (default: file).
        Used after a write operation succeeds to grant future permission.
        """
        self.entries[path.resolve()] = is_dir

    def remove(self, path: Path):
        """Remove a path from the allowlist, whether recorded as a file or a directory."""
        self.entries.pop(path.resolve(), None)

    def has(self, path: Path) -> bool:
        """Check if a path is in the allowlist. Directory entries match everything under them."""
        resolved = path.resolve()
        return any(resolved == p or (is_dir and resolved.is_relative_to(p)) for p, is_dir in self.entries.items())


class CommandExecutionAllowList:
    """
    Track commands that the agent is allowed to execute.
    Should be stored in the agent's state, not global.
    """
    def __init__(self):
        self._allowlist = {
            "ls",
            "wc",
            "echo",
            "pwd",
            "tree",
            "date",
            "which",
            "whoami",
            "uptime",
            "df",
            "free",
            "ps",
            "top",
            "netstat",
            "ifconfig",
            "ping",
            "traceroute",
            "curl",
            "wget",
            "dig",
            "nslookup",
            "ip",
            "ss",
            "lsof",
            "lspci",
            "lscpu",
            "lsusb",
            "lsblk",
            "dmesg",
            "journalctl",
            "lsb_release",
            "uname",

            "grep",
            "head",
            "tail",
            "sed", 
            "cat",
            "nl", 

            "nvidia-smi",

            "python -m unittest",
            "python3 -m unittest",
            "python -m pytest",
            "python3 -m pytest",

            "git status",
            "git --no-pager status",
            "git log",
            "git --no-pager log",
            "git diff",
            "git --no-pager diff",
            "git show",
            "git --no-pager show",
        }
    
    @property
    def allowlist(self) -> set[str]:
        return self._allowlist
    
    # Multi-token allowlist entries act as command prefixes, so a command is
    # auto-approved when it starts with one of them (e.g. "git diff --cached"
    # starts with the allowlisted "git diff"). Matching on whole tokens keeps the
    # boundary on argument edges, so "git status" does not match "git statusx".
    @property
    def allowlist_prefix(self) -> Sequence[tuple[str, ...]]:
        return [tuple(entry.split()) for entry in self._allowlist if " " in entry]
    
    def add(self, command: str):
        """Add a command to the allowlist."""
        self._allowlist.add(command)

    def remove(self, command: str):
        """Remove a command from the allowlist."""
        self._allowlist.discard(command)
    
    def has(self, command: str) -> bool:
        """Check if a command is in the allowlist."""
        return command in self._allowlist

@dataclass
class Policy(StateEntry):
    """Confirmed write/command grants. Runtime-only (default lifetime): restarting
    the agent starts from a clean allowlist rather than reviving old approvals."""

    write_allowlist: WriteAllowList = field(default_factory=WriteAllowList)
    command_allowlist: CommandExecutionAllowList = field(default_factory=CommandExecutionAllowList)

def get_policy(ctx: Context) -> Policy:
    return get_policy_from_agent(ctx.agent)

def get_policy_from_agent(agent: Agent[Agent.T.Init]) -> Policy:
    """Get or create a Policy stored in the agent's state."""
    return agent.get_state_entry("__builtin_tool_policy", Policy)

def default_tool_commands() -> list[Command]:
    from ..command import Command, CommandRegistry

    _policy = get_policy_from_agent     # shorthand

    def _add_cmd_allowlist(agent: Agent[Agent.T.Init], args: list[str]) -> None:
        """Add a command to the allowlist."""
        if len(args) != 1:
            agent.error("Usage: policy cmd_allowlist_add <command>")
            return
        _policy(agent).command_allowlist.add(args[0])

    def _remove_cmd_allowlist(agent: Agent[Agent.T.Init], args: list[str]) -> None:
        """Remove a command from the allowlist."""
        if len(args) != 1:
            agent.error("Usage: policy cmd_allowlist_remove <command>")
            return
        _policy(agent).command_allowlist.remove(args[0])

    def _add_path_allowlist(agent: Agent[Agent.T.Init], args: list[str]) -> None:
        """Add a path (file or directory) to the write allowlist. Directory entries match everything under them."""
        if len(args) != 1:
            agent.error("Usage: policy path_allowlist_add <path>")
            return
        resolved = Path(args[0]).resolve()
        _policy(agent).write_allowlist.add(resolved, is_dir=resolved.is_dir())

    def _remove_path_allowlist(agent: Agent[Agent.T.Init], args: list[str]) -> None:
        """Remove a path from the write allowlist."""
        if len(args) != 1:
            agent.error("Usage: policy path_allowlist_remove <path>")
            return
        _policy(agent).write_allowlist.remove(Path(args[0]).resolve())

    def _list_path_allowlist(agent: Agent[Agent.T.Init]):
        """List the paths in the write allowlist. Directory entries are marked with a trailing '/'."""
        agent_workdir = agent.workspace.workdir.resolve()
        lines = ["Write Allowlist:"]
        for p, is_dir in sorted(_policy(agent).write_allowlist.entries.items()):
            shown = p.relative_to(agent_workdir) if p.is_relative_to(agent_workdir) else p
            lines.append(f"  {shown}{'/' if is_dir else ''}")
        agent.info("\n".join(lines))

    def _list_command_allowlist(agent: Agent[Agent.T.Init]):
        """List the commands in the command allowlist."""
        lines = ["Command Allowlist:"]
        for cmd in sorted(_policy(agent).command_allowlist.allowlist):
            lines.append(f"  {cmd}")
        agent.info("\n".join(lines))

    registry = CommandRegistry().register(
        Command(name="cmd_allowlist_add", handler=_add_cmd_allowlist),
        Command(name="cmd_allowlist_remove", handler=_remove_cmd_allowlist),
        Command(name="cmd_allowlist", handler=_list_command_allowlist),
        Command(name="path_allowlist_add", handler=_add_path_allowlist),
        Command(name="path_allowlist_remove", handler=_remove_path_allowlist),
        Command(name="path_allowlist", handler=_list_path_allowlist),
    )

    description = "Manage built-in tool policies (write and command allowlists)."
    description_long = f"{description}\nUsage:\n  policy <subcommand> [arguments]\n" + Command._format_subcommands(registry)
    return [
        Command(
            name="policy",
            handler=registry,
            description=description,
            description_long=description_long,
        )
    ]
