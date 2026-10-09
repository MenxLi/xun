"""
The bash command execution is highly un-controlled and nearly impossible to control safely without external sandboxing.
The code in this module are best-effort assessments of command risk and should not be relied upon for complete safety.
"""
from dataclasses import dataclass
import shlex
import shutil
from pathlib import Path
from pydantic import BaseModel
from typing import Callable, Optional, Literal, Sequence
from ..toolcall import ToolCallContext
from .common import resolve_path, get_policy

SHELL_OPERATORS = {";", "&&", "&", "||", "|", ">", ">>", "<", "<<", ">&", "<&", "(", ")"}
AUTO_APPROVED_SHELL_OPERATORS = {";", "&&", "||", "|", "(", ")"}
COMMAND_CHAIN_OPERATORS = {";", "&&", "||", "|"}
SAFE_REDIRECTION_TARGETS = {"/dev/null"}

UNSURE_KIND = Literal[
    "install_package_outside_allowed_paths", 
    "change_system_status", 
    "modify_existing_files", 
    "unable_to_assess", 
    "other",
]
MUST_CONFIRM_UNSURE_KINDS: set[UNSURE_KIND] = {
    "install_package_outside_allowed_paths",
    "change_system_status",
    # deliberate exclusion of "unable_to_assess" for `auto_confirm` to be handy
}
class RiskAccessResult(BaseModel):
    policy: Literal['allow', 'unsure', 'reject']
    reason: Optional[str] = None
    unsure_kind: Optional[UNSURE_KIND] = None
def agent_risk_access(
    ctx: ToolCallContext,
    cmd: str, 
    workdir: Path,
    extra_allowed_paths: Sequence[Path] = (),
    ) -> RiskAccessResult:
    from .. import Agent, NullDisplay, ToolBox
    from .fs import fs_read_file, fs_list, fs_glob_files, fs_grep_files
    base = Agent(
        name="Command Risk Assessment", 
        display=NullDisplay(), 
        toolbox = ToolBox().register(
            fs_read_file, fs_list, fs_glob_files, fs_grep_files
            ),
        api_call_semaphore=ctx.agent.api_call_semaphore, 
        workspace=ctx.agent.workspace,
        cancel_event=ctx.agent.cancel_event.derive(),
    )
    # internal helper agent: no extensions (minimal privilege)
    base.config.enable_extensions = False

    agent: "Agent[Agent.T.Init]" = base.system(
        "You are an agent that is responsible for accessing shell commands. "
        "The command will be run under given working directory. "
        "You must determine whether the command is safe to execute (allow), requires user confirmation (unsure), or should be rejected outright (reject).\n\n"

        "The command should be rejected if it is potentially harmful, or work outside the working directory or allowed paths (subdirectories are allowed). \n"
        "A command is doomed to be potentially harmful if it may disrupt the system or irreversibly modify important files "
        "(installing trusted packages may be exempt, return unsure for these to allow user confirmation). \n"
        "The command is considered safe if it is a readonly command. \n"
        "Otherwise, it should be confirmed with the user before execution by assessing it as `unsure`. \n\n"

        "You have tools to read files (only within the allowed paths), "
        "you should avoid using them unless they are absolutely necessary to determine the risk of the command. \n\n"
        "If you determine that the command is safe, you can output a reason as null. "
        "Otherwise, you should provide a concise (less than 20 words) reason for your decision, "
        "moreover, if you are unsure, you should indicate the reason kind from the predefined categories. \n"
        "If using tools, restrict to at most 3 tool calls, otherwise return unsure with your current assessment."
    ).instruct(
        f"Given the command: `{cmd}`\n"
        f"Current working directory: `{workdir} (absolute path: {workdir.resolve()})`\n"
        f"Extra allowed paths: `{extra_allowed_paths}`\n"
        f"Please determine the risk access policy for this command. "
    ).initialize()
    res = agent.execute(schema=RiskAccessResult, max_iterations=8)
    if res.is_err():
        return RiskAccessResult(policy='unsure', reason=f"Failed to assess command risk: {res.unwrap_err()}", unsure_kind="unable_to_assess")
    res_ok = res.unwrap()
    if res_ok.policy == 'unsure' and res_ok.unsure_kind is None:
        res_ok.unsure_kind = "other"
    return res_ok

@dataclass(frozen=True)
class CommandSegment:
    """
    Represents a shell command segment (executable + arguments) that is delimited by command operators.
    This allows checking allowlists based on full command invocations, e.g., "python -m unittest".
    """
    tokens: tuple[str, ...]

    @property
    def executable(self) -> str:
        """The first token, which is the command name or path."""
        return self.tokens[0] if self.tokens else ""

    @property
    def command_str(self) -> str:
        """Reconstruct the command as it would appear."""
        # Use a simple space join. This is for comparison against allowlist entries.
        # Note: quoting and whitespace may differ, but we expect allowlist entries to be in a normalized form.
        return " ".join(self.tokens)

    def _matches_allowlist_prefix(self, prefix: tuple[str, ...]) -> bool:
        """Check if the segment starts with the given prefix at token boundaries."""
        return len(self.tokens) >= len(prefix) and self.tokens[:len(prefix)] == prefix

    def is_allowlisted(self, ctx: ToolCallContext) -> bool:
        """
        Check if this command segment is allowed.
        It is allowed if:
          - The executable (first token) is in the command allowlist.
          - OR, the command starts with a multi-token entry in the allowlist
            (e.g. "git diff --cached" starts with the allowlisted "git diff").
        """
        policy = get_policy(ctx)
        if self.executable in policy.command_allowlist.allowlist:
            return True
        if self.command_str in policy.command_allowlist.allowlist:
            return True
        return any(
            self._matches_allowlist_prefix(prefix) for prefix in policy.command_allowlist.allowlist_prefix
        )


@dataclass(frozen=True)
class ExecutableSpec:
    value: str

    @property
    def path(self) -> Path:
        return Path(self.value)

    @property
    def is_bare_command(self) -> bool:
        return self.path.name == self.value


@dataclass(frozen=True)
class CommandSpec:
    command_line: str
    argv: tuple[str, ...]
    commands: tuple[ExecutableSpec, ...]
    segments: tuple[CommandSegment, ...]

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


def _extract_exes(argv: list[str]) -> tuple[ExecutableSpec, ...]:
    """Extract each executable token (the head of every command in the chain)."""
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


def _extract_segments(argv: list[str]) -> tuple[CommandSegment, ...]:
    """Extract full command segments, splitting on chain operators."""
    segments = []
    current: list[str] = []

    def flush():
        if current:
            segments.append(CommandSegment(tuple(current)))
            current.clear()

    for token in argv:
        if token in COMMAND_CHAIN_OPERATORS:
            flush()
            continue
        if token in ("(", ")"):
            continue
        # Include all other tokens in the segment (operators like >, <, etc.)
        current.append(token)

    flush()
    return tuple(segments)


def _parse_command_spec(command_line: str) -> CommandSpec:
    if not command_line.strip():
        raise ValueError("Command must not be empty.")

    lexer = shlex.shlex(command_line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    argv = list(lexer)
    if not argv:
        raise ValueError("Command must not be empty.")

    commands = _extract_exes(argv)
    if not commands:
        raise ValueError("Command must contain an executable.")

    segments = _extract_segments(argv)

    return CommandSpec(
        command_line=command_line,
        argv=tuple(argv),
        commands=commands,
        segments=segments,
    )


def _first_matching_command(
    spec: CommandSpec,
    predicate: Callable[[ExecutableSpec], bool],
) -> ExecutableSpec | None:
    return next((command for command in spec.commands if predicate(command)), None)


def _first_matching_segment(
    segments: tuple[CommandSegment, ...],
    predicate: Callable[[CommandSegment], bool],
) -> CommandSegment | None:
    return next((seg for seg in segments if predicate(seg)), None)


def _command_path_reason(spec: CommandSpec) -> str | None:
    if _first_matching_command(spec, lambda command: not command.is_bare_command):
        return "command chain includes a path-based command"
    return None


def _shell_syntax_reasons(command_line: str) -> tuple[str, ...]:
    reasons: list[str] = []
    if "`" in command_line:
        reasons.append("uses backtick command substitution")
    if "\n" in command_line or "\r" in command_line:
        reasons.append("uses line-separated commands")
    return tuple(reasons)


def _command_substitutions(command_line: str) -> tuple[str, ...]:
    substitutions: list[str] = []
    index = 0
    quote: str | None = None

    while index < len(command_line):
        character = command_line[index]
        if character == "\\" and quote != "'":
            index += 2
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            index += 1
            continue
        if quote != "'" and command_line.startswith("$(", index):
            start = index + 2
            depth = 1
            cursor = start
            inner_quote: str | None = None
            while cursor < len(command_line) and depth:
                inner_character = command_line[cursor]
                if inner_character == "\\" and inner_quote != "'":
                    cursor += 2
                    continue
                if inner_character in {"'", '"'}:
                    if inner_quote is None:
                        inner_quote = inner_character
                    elif inner_quote == inner_character:
                        inner_quote = None
                elif inner_quote != "'" and command_line.startswith("$(", cursor):
                    depth += 1
                    cursor += 1
                elif inner_quote is None and inner_character == ")":
                    depth -= 1
                    if depth == 0:
                        substitutions.append(command_line[start:cursor])
                        index = cursor
                cursor += 1
        index += 1

    return tuple(substitutions)


def _command_substitution_reasons(ctx: ToolCallContext, command_line: str) -> tuple[str, ...]:
    reasons: list[str] = []
    for substitution in _command_substitutions(command_line):
        try:
            policy = _confirmation_policy(ctx, _parse_command_spec(substitution))
        except ValueError:
            reasons.append("contains an invalid command substitution")
            continue
        if policy.requires_confirmation:
            reasons.append(f"command substitution requires confirmation: $({substitution})")
    return tuple(reasons)


def _confirmation_policy(ctx: ToolCallContext, spec: CommandSpec) -> ConfirmationPolicy:
    reasons: list[str] = []

    # Check if any segment is not allowlisted.
    unallowlisted_segment = _first_matching_segment(spec.segments, lambda seg: not seg.is_allowlisted(ctx))
    path_reason = _command_path_reason(spec)
    syntax_reasons = _shell_syntax_reasons(spec.command_line)
    substitution_reasons = _command_substitution_reasons(ctx, spec.command_line)

    if unallowlisted_segment is not None:
        reasons.append(f"command '{unallowlisted_segment.executable}' is not allowlisted in full command '{unallowlisted_segment.command_str}'")
    if spec.disallowed_operators:
        reasons.append(f"uses shell operators requiring confirmation ({', '.join(spec.disallowed_operators)})")
    if path_reason is not None:
        reasons.append(path_reason)
    reasons.extend(syntax_reasons)
    reasons.extend(substitution_reasons)

    rejection_message = None
    if unallowlisted_segment is not None:
        rejection_message = f"Command '{unallowlisted_segment.executable}' is not allowlisted."
    elif spec.disallowed_operators:
        rejection_message = "Shell redirections and background operators are not allowed without confirmation, except for exact safe forms like 2>&1 and >/dev/null."
    elif path_reason is not None:
        rejection_message = "Path-based commands (absolute or relative) are not allowed without confirmation."
    elif syntax_reasons:
        rejection_message = "Backtick command substitution and line-separated commands are not allowed without confirmation."
    elif substitution_reasons:
        rejection_message = "A command substitution contains a command that is not allowlisted."

    return ConfirmationPolicy(
        allow_unlisted=(unallowlisted_segment is not None) or (path_reason is not None),
        reasons=tuple(reasons),
        rejection_message=rejection_message,
    )


def _confirm_command_execution(
    ctx: ToolCallContext, 
    spec: CommandSpec, 
    policy: ConfirmationPolicy, 
    workdir_resolved: Path
    ) -> bool:
    if not policy.requires_confirmation:
        return False
    
    agent_check_res = agent_risk_access(
        ctx,
        spec.command_line,
        workdir=workdir_resolved,
        extra_allowed_paths=[ctx.agent.workspace.tempdir.path],
    )
    if agent_check_res.policy == 'allow':
        return True
    
    # In auto-confirm mode, ctx.agent.get_confirm returns the default choice:
    # 'unsure' defaults to Yes (allowed), 'reject' defaults to No (raises below).
    reasons_str = " and ".join(policy.reasons)
    message = f"Confirming on command `{spec.command_line}` because it {reasons_str}."
    if policy.rejection_message:
        message += f"\n{policy.rejection_message}"
    if agent_check_res.reason:
        message += f"\n(Risk assessment: {agent_check_res.reason})"
        if agent_check_res.unsure_kind:
            message += f" [{agent_check_res.unsure_kind}]"
    outcome = ctx.agent.get_choice(
        "Allow command? (choose 'Other' to reject with a message)", ["Yes", "No"],
        message=message,
        title="Command Confirmation" if agent_check_res.policy == 'unsure' else "Dangerous Command Confirmation",
        subtitle=ctx.agent.name,
        default="Yes" if agent_check_res.policy == 'unsure' else "No",
        allow_extra=True,
        skip_auto_confirm=(
            agent_check_res.policy == 'unsure'
            and agent_check_res.unsure_kind in MUST_CONFIRM_UNSURE_KINDS
            )
    )
    if outcome.choice != "Yes":
        rejection = (
            f"user message: {outcome.choice}"
            if outcome.choice != "No"
            else f"risk assessment: {agent_check_res.reason}"
        )
        raise RuntimeError(f"Command `{spec.command_line}` was rejected by confirmation. ({rejection})")

    return policy.allow_unlisted


def _resolve_executable(command: ExecutableSpec, allow_unlisted: bool, cwd: Path) -> str | None:
    raw_command = command.value
    if not raw_command:
        raise ValueError("Command must not be empty.")

    if not command.is_bare_command:
        if not allow_unlisted:
            raise ValueError("A path-based command requires explicit confirmation before execution.")
        path = command.path if command.path.is_absolute() else cwd / command.path
        if not path.is_file():
            raise ValueError(f"Command '{raw_command}' was not found.")
        return str(path)

    executable = shutil.which(raw_command)
    if executable is not None:
        return executable

    # Bare shell builtins such as `cd` are resolved by the invoked bash.
    return None


def prepare_command(
    ctx: ToolCallContext,
    command: str,
    cd: Optional[str] = None,
) -> Path:
    """Validate the command and working directory, obtaining approval before execution."""
    spec = _parse_command_spec(command)
    policy = _confirmation_policy(ctx, spec)

    # Determine workdir with safety validation
    cwd: Path
    if cd is not None:
        resolved = resolve_path(ctx, cd, raise_on_invalid=False)
        if not resolved.valid:
            raise ValueError(
                f"Workdir `{cd}` is not within agent's workspace "
                f"(workdir: {ctx.agent.workspace.workdir}, or its temporary directory)."
            )
        cwd = resolved.path
    else:
        cwd = ctx.agent.workspace.workdir

    allow_unlisted = _confirm_command_execution(ctx, spec, policy, workdir_resolved=cwd)
    for exe in spec.commands:
        _resolve_executable(exe, allow_unlisted=allow_unlisted, cwd=cwd)

    return cwd