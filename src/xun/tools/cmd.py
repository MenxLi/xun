from dataclasses import dataclass
import os
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Callable
from typing_extensions import TypedDict

from ..context import tool_call_context

CMD_ALLOWLIST = {
    "ls",
    "wc", 
    "echo",
    "pwd",
    "tree", 
    "date",
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
    "dmesg",
    "journalctl",
    "lsb_release",
    "uname",

    "grep",
    "head",
    "tail",
    "cat",
}

SHELL_OPERATORS = {";", "&&", "&", "||", "|", ">", ">>", "<", "<<", ">&", "<&", "(", ")"}
AUTO_APPROVED_SHELL_OPERATORS = {";", "&&", "||", "|", "(", ")"}
COMMAND_CHAIN_OPERATORS = {";", "&&", "||", "|"}
SAFE_REDIRECTION_TARGETS = {"/dev/null"}


@dataclass(frozen=True)
class ExecutableSpec:
    value: str

    @property
    def path(self) -> Path:
        return Path(self.value)

    @property
    def is_bare_command(self) -> bool:
        return self.path.name == self.value

    @property
    def is_absolute_path(self) -> bool:
        return self.path.is_absolute()

    @property
    def is_allowlisted(self) -> bool:
        return self.is_bare_command and self.value in CMD_ALLOWLIST


@dataclass(frozen=True)
class CommandSpec:
    command_line: str
    argv: tuple[str, ...]
    commands: tuple[ExecutableSpec, ...]

    @property
    def disallowed_operators(self) -> tuple[str, ...]:
        return _disallowed_shell_operators(self.argv)


def _safe_redirection_span(argv: tuple[str, ...], index: int) -> int | None:
    if index + 1 < len(argv) and argv[index] == ">" and argv[index + 1] in SAFE_REDIRECTION_TARGETS:
        return 2

    if (
        index + 2 < len(argv)
        and argv[index] in {"1", "2"}
        and argv[index + 1] == ">"
        and argv[index + 2] in SAFE_REDIRECTION_TARGETS
    ):
        return 3

    if index + 2 < len(argv) and argv[index] == "2" and argv[index + 1] == ">&" and argv[index + 2] == "1":
        return 3

    return None


def _disallowed_shell_operators(argv: tuple[str, ...]) -> tuple[str, ...]:
    disallowed: set[str] = set()
    index = 0

    while index < len(argv):
        token = argv[index]

        if token in AUTO_APPROVED_SHELL_OPERATORS:
            index += 1
            continue

        safe_redirection_span = _safe_redirection_span(argv, index)
        if safe_redirection_span is not None:
            index += safe_redirection_span
            continue

        if token in SHELL_OPERATORS:
            disallowed.add(token)

        index += 1

    return tuple(sorted(disallowed))


@dataclass(frozen=True)
class ConfirmationPolicy:
    allow_unlisted: bool
    reasons: tuple[str, ...]
    rejection_message: str | None

    @property
    def requires_confirmation(self) -> bool:
        return bool(self.reasons)


def _resolve_executable(command: ExecutableSpec, allow_unlisted: bool) -> str | None:
    raw_command = command.value
    if not raw_command:
        raise ValueError("Command must not be empty.")

    if not command.is_bare_command:
        if not allow_unlisted or not command.is_absolute_path:
            raise ValueError("Command must be a bare executable name unless explicitly confirmed as an absolute path.")
        if not command.path.is_file():
            raise ValueError(f"Command '{raw_command}' was not found.")
        return str(command.path)

    executable = shutil.which(raw_command)
    if executable is not None:
        return executable

    # Bare shell builtins such as `cd` are resolved by the invoked shell.
    return None


def _resolve_commands(spec: CommandSpec, allow_unlisted: bool) -> None:
    for command in spec.commands:
        _resolve_executable(command, allow_unlisted=allow_unlisted)


def _extract_commands(argv: list[str]) -> tuple[ExecutableSpec, ...]:
    commands: list[ExecutableSpec] = []
    expect_command = True

    for token in argv:
        if token == "(":
            expect_command = True
            continue
        if token == ")":
            continue
        if token in COMMAND_CHAIN_OPERATORS:
            expect_command = True
            continue
        if expect_command and token not in SHELL_OPERATORS:
            commands.append(ExecutableSpec(token))
            expect_command = False

    return tuple(commands)


def _parse_command_spec(command_line: str) -> CommandSpec:
    if not command_line.strip():
        raise ValueError("Command must not be empty.")

    lexer = shlex.shlex(command_line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    argv = list(lexer)
    if not argv:
        raise ValueError("Command must not be empty.")

    commands = _extract_commands(argv)
    if not commands:
        raise ValueError("Command must contain an executable.")

    return CommandSpec(command_line=command_line, argv=tuple(argv), commands=commands)


def _first_matching_command(
    spec: CommandSpec,
    predicate: Callable[[ExecutableSpec], bool],
) -> ExecutableSpec | None:
    return next((command for command in spec.commands if predicate(command)), None)


def _command_path_reason(spec: CommandSpec) -> str | None:
    if _first_matching_command(spec, lambda command: not command.is_bare_command and not command.is_absolute_path):
        return "command chain includes a non-bare command path"
    if _first_matching_command(spec, lambda command: command.is_absolute_path):
        return "command chain includes an absolute path command"
    return None


def _shell_syntax_reasons(command_line: str) -> tuple[str, ...]:
    reasons: list[str] = []
    if "`" in command_line:
        reasons.append("uses backtick command substitution")
    if "\n" in command_line or "\r" in command_line:
        reasons.append("uses line-separated commands")
    return tuple(reasons)


def _confirmation_policy(spec: CommandSpec) -> ConfirmationPolicy:
    reasons: list[str] = []
    unallowlisted_command = _first_matching_command(spec, lambda command: not command.is_allowlisted)
    path_reason = _command_path_reason(spec)
    syntax_reasons = _shell_syntax_reasons(spec.command_line)

    if unallowlisted_command is not None:
        reasons.append("command chain includes commands outside the allowlist")
    if spec.disallowed_operators:
        reasons.append(f"uses shell operators requiring confirmation ({', '.join(spec.disallowed_operators)})")
    if path_reason is not None:
        reasons.append(path_reason)
    reasons.extend(syntax_reasons)

    rejection_message = None
    if path_reason == "command chain includes a non-bare command path":
        rejection_message = "Only bare executable names or explicitly confirmed absolute command paths are allowed."
    elif unallowlisted_command is not None:
        rejection_message = f"Command '{unallowlisted_command.value}' is not allowed."
    elif spec.disallowed_operators:
        rejection_message = "Shell redirections and background operators are not allowed without confirmation, except for exact safe forms like 2>&1 and >/dev/null."
    elif path_reason is not None:
        rejection_message = "Absolute command paths are not allowed without confirmation."
    elif syntax_reasons:
        rejection_message = "Backtick command substitution and line-separated commands are not allowed without confirmation."

    return ConfirmationPolicy(
        allow_unlisted=(unallowlisted_command is not None) or (path_reason is not None),
        reasons=tuple(reasons),
        rejection_message=rejection_message,
    )


def _confirm_command_execution(spec: CommandSpec, policy: ConfirmationPolicy) -> bool:
    if not policy.requires_confirmation:
        return False

    ctx = tool_call_context.get()
    assert ctx is not None, "Tool call context is required for command execution confirmation."

    reasons_str = " and ".join(policy.reasons)
    message = f"Confirming on command `{spec.command_line}` because it {reasons_str}."
    if policy.rejection_message:
        message += f"\n{policy.rejection_message}"
    if not ctx.display.get_confirm(
        "Allow command?", message,
        title="Command Execution Confirmation",
        subtitle=ctx.agent.name if ctx else None,
        default=True,
    ):
        raise RuntimeError(f"Command `{spec.command_line}` was rejected by user confirmation.")

    return policy.allow_unlisted

def _soft_kill_process(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        process.terminate()
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _hard_kill_process(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        process.kill()
        return

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_shell_command(spec: CommandSpec, timeout: float) -> subprocess.CompletedProcess[str]:
    shell_executable = os.environ.get("SHELL")
    popen_kwargs = {
        "shell": True,
        "text": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": os.environ.copy(),
        "cwd": os.getcwd(),
    }
    if shell_executable:
        popen_kwargs["executable"] = shell_executable
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(spec.command_line, **popen_kwargs)  # nosec B602
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _soft_kill_process(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _hard_kill_process(process)
            stdout, stderr = process.communicate()

        raise RuntimeError(
            f"Command `{spec.command_line}` timed out after {timeout:g}s and was terminated."
        )

    return subprocess.CompletedProcess(
        args=spec.command_line,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )

class CmdExecResult(TypedDict):
    args: str
    stdout: str
    stderr: str
    returncode: int
# Unlisted commands, unsupported shell operators, and absolute command paths still require confirmation.
def cmd_exec(command: str, timeout: float = 300) -> CmdExecResult:
    """
    Runs a command and returns its output.
    Commands are always run through the current shell with inherited environment variables.

    The command runs in the current process working directory, and cannot change it persistently.

    The command is running in a blocking way, will wait until the command finishes before return. 
    Commands will be terminated if they exceed the timeout in seconds.

    if need to run non-blocking command, please use `nohup` or `&` operator and confirm the shell operators.
    Do remember to check and cleanup the background processes if run non-blocking, the system won't do it for you.
    """
    spec = _parse_command_spec(command)
    policy = _confirmation_policy(spec)
    allow_unlisted = _confirm_command_execution(spec, policy)
    _resolve_commands(spec, allow_unlisted=allow_unlisted)
    try:
        result = _run_shell_command(spec, timeout=timeout)
    except KeyboardInterrupt:
        raise RuntimeError(f"Command `{spec.command_line}` was interrupted by user.")

    return CmdExecResult(
        args=spec.command_line,
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
        returncode=result.returncode,
    )

def expose_cmd_tools() -> list[Callable]:
    import rich
    if os.name == "nt":
        rich.print("[Warning] The cmd_exec tool is not available on Windows. Skip registering it.")
        return []
    return [cmd_exec]