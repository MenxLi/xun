"""Blocking commands and agent-owned background bash jobs."""

from contextlib import ExitStack
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
from tempfile import TemporaryFile
from threading import Event, Lock, Thread
import time
from typing import BinaryIO, Callable, Literal, Sequence
import uuid

from typing_extensions import TypedDict

from ..agent_state import RuntimeEntry
from ..toolcall import ToolCallContext
from ..types import CancelledError
from .cmd_policy import prepare_command

_POOL_CREATION_LOCK = Lock()


class CmdExecResult(TypedDict):
    stdout: str
    stderr: str
    returncode: int
    duration: str


class CommandHandle(TypedDict):
    id: str


class CommandInfo(CommandHandle):
    command: str
    status: Literal["running", "finished"]


class CommandOutput(CommandInfo):
    stdout: str
    stderr: str
    returncode: int | None
    duration: str


class WaitTimeout(CommandHandle):
    status: Literal["timeout"]
    notice: str


def _validate_timeout(timeout: float | None) -> None:
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("timeout must be finite and non-negative, or null.")


def _validate_output_size(max_output_size: int | None) -> None:
    if max_output_size is not None and max_output_size <= 0:
        raise ValueError("max_output_size must be positive, or null.")


def truncate_output(text: str, max_output_size: int | None) -> str:
    """Preserve head and tail, with a marker in between, when over the limit."""
    _validate_output_size(max_output_size)
    if max_output_size is not None and len(text) > max_output_size:
        head = (max_output_size + 1) // 2
        tail = max_output_size // 2
        return f"{text[:head]}\n[... truncated output ...]\n{text[-tail:] if tail else ''}"
    return text


def _duration(elapsed: float) -> str:
    return f"{elapsed:.2f}s" if elapsed < 60 else f"{int(elapsed)//60}m{int(elapsed)%60:02d}s"


def _environment(overrides: dict[str, str] | None) -> dict[str, str]:
    return os.environ | (overrides or {})


def _signal_process(process: subprocess.Popen[str] | subprocess.Popen[bytes], hard: bool = False) -> None:
    try:
        if os.name == "nt":
            process.kill() if hard else process.terminate()
        else:
            os.killpg(process.pid, signal.SIGKILL if hard else signal.SIGTERM)
    except ProcessLookupError:
        pass


def _terminate_and_collect(process: subprocess.Popen[str]) -> tuple[str, str]:
    _signal_process(process)
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        _signal_process(process, hard=True)
        return process.communicate()


def run_command(
    target: str | Sequence[str],
    timeout: float,
    cwd: Path,
    env_overrides: dict[str, str] | None,
    cancel_check: Callable[[], bool],
) -> subprocess.CompletedProcess[str]:
    """Run a string through bash, or execute an argv sequence without a shell."""
    _validate_timeout(timeout)
    command_line = target if isinstance(target, str) else shlex.join(target)
    process = subprocess.Popen(
        target,
        shell=isinstance(target, str),
        executable=(shutil.which("bash") or "/bin/sh") if isinstance(target, str) else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_environment(env_overrides),
        cwd=cwd,
        start_new_session=os.name != "nt",
    )  # nosec B602
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel_check():
                    _terminate_and_collect(process)
                    raise CancelledError(f"Command `{command_line}` was cancelled by user.")
                if time.monotonic() >= deadline:
                    raise
    except subprocess.TimeoutExpired:
        _terminate_and_collect(process)
        raise RuntimeError(f"Command `{command_line}` timed out after {timeout:g}s and was terminated.")
    except KeyboardInterrupt:
        _terminate_and_collect(process)
        raise
    return subprocess.CompletedProcess(command_line, process.returncode, stdout, stderr)


@dataclass
class CommandJob:
    command: str
    process: subprocess.Popen[bytes]
    stdout: BinaryIO
    stderr: BinaryIO
    files: ExitStack
    started: float
    finished: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    ended: float | None = None
    error: OSError | None = None
    closed: bool = False

    @classmethod
    def start(cls, command: str, cwd: Path, envs: dict[str, str] | None) -> "CommandJob":
        with ExitStack() as files:
            stdout = files.enter_context(TemporaryFile())
            stderr = files.enter_context(TemporaryFile())
            started = time.monotonic()
            process = subprocess.Popen(
                command, shell=True, executable=shutil.which("bash") or "/bin/sh",
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                cwd=cwd, env=_environment(envs),
                start_new_session=True,
            )  # nosec B602
            job = cls(command, process, stdout, stderr, files.pop_all(), started)
        try:
            Thread(target=job._watch, name=f"bash-{process.pid}", daemon=True).start()
        except RuntimeError:
            job.finished.set()
            job.close()
            raise
        return job

    def _watch(self) -> None:
        try:
            self.process.wait()
            _signal_process(self.process, hard=True)
        except OSError as error:
            self.error = error
        finally:
            self.ended = time.monotonic()
            self.finished.set()

    def _read(self, stream: BinaryIO, max_output_size: int | None) -> str:
        size = os.fstat(stream.fileno()).st_size
        # pread does not move the shared file offset used by the child to write.
        window = size if max_output_size is None else max_output_size * 4
        data = os.pread(stream.fileno(), min(size, window), 0)
        if size > window:
            data += os.pread(stream.fileno(), window, max(window, size - window))
        return truncate_output(data.decode("utf-8", errors="replace").strip(), max_output_size)

    def output(self, identifier: str, max_output_size: int | None) -> CommandOutput:
        with self.lock:
            if self.closed:
                raise ValueError(f"Command no longer available: {identifier}")
            if self.error is not None:
                raise RuntimeError(f"Failed to monitor command: {identifier}") from self.error
            done = self.finished.is_set()
            return CommandOutput(
                id=identifier, command=self.command, status="finished" if done else "running",
                stdout=self._read(self.stdout, max_output_size),
                stderr=self._read(self.stderr, max_output_size),
                returncode=self.process.returncode if done else None,
                duration=_duration((self.ended if self.ended is not None else time.monotonic()) - self.started),
            )

    def stop(self) -> None:
        with self.lock:
            if self.closed:
                raise ValueError("Command no longer available.")
            if self.finished.is_set() and self.error is None:
                return
            _signal_process(self.process)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # Kill surviving descendants even if the shell has already exited.
                _signal_process(self.process, hard=True)
            self.process.wait()
            self.finished.wait()

    def close(self) -> None:
        self.stop()
        with self.lock:
            self.files.close()
            self.closed = True


@dataclass
class CommandPool(RuntimeEntry[dict[str, CommandJob]]):
    lock: Lock = field(default_factory=Lock, repr=False, compare=False)
    closed: bool = False

    @classmethod
    def of(cls, ctx: ToolCallContext) -> "CommandPool":
        def create() -> CommandPool:
            pool = cls({})
            ctx.agent.hooks.before_finalize.add(lambda _: pool.shutdown())
            return pool
        with _POOL_CREATION_LOCK:
            return ctx.agent.state.get_entry("__bash_jobs", or_else=create)

    def get(self, identifier: str) -> CommandJob:
        with self.lock:
            job = self.value.get(identifier)
        if job is None:
            raise ValueError(f"Command not found: {identifier}")
        return job

    def collect(self, identifier: str, max_output_size: int | None) -> CmdExecResult:
        with self.lock:
            job = self.value.get(identifier)
            if job is None:
                raise ValueError(f"Command not found: {identifier}")
            output = job.output(identifier, max_output_size)
            if output["returncode"] is None:
                raise RuntimeError(f"Command is still running: {identifier}")
            job.close()
            del self.value[identifier]
        return CmdExecResult(
            stdout=output["stdout"], stderr=output["stderr"],
            returncode=output["returncode"], duration=output["duration"],
        )

    def shutdown(self) -> None:
        with self.lock:
            self.closed = True
            jobs = list(self.value.values())
            self.value.clear()
        with ExitStack() as cleanup:
            for job in jobs:
                cleanup.callback(job.close)


def bash(
    ctx: ToolCallContext,
    command: str,
    timeout: float = 300,
    cd: str | None = None,
    envs: dict[str, str] | None = None,
    max_output_size: int | None = 16_000,
) -> CmdExecResult:
    """
    Run through bash (or sh), returning output, exit code and duration.
    `cd` defaults to the workspace; `envs` overrides inherited variables for this call.
    Timeout (seconds) or cancellation terminates the command.
    `max_output_size` limits characters per stream, preserving head/tail plus a marker;
    null disables truncation. For non-blocking execution, use `bash_background`.
    """
    _validate_timeout(timeout)
    _validate_output_size(max_output_size)
    cwd = prepare_command(ctx, command, cd)
    start = time.monotonic()
    result = run_command(command, timeout, cwd, envs, ctx.agent.cancel_event.is_set)
    return CmdExecResult(
        stdout=truncate_output(result.stdout.strip(), max_output_size),
        stderr=truncate_output(result.stderr.strip(), max_output_size),
        returncode=result.returncode, duration=_duration(time.monotonic() - start),
    )


def bash_background(
    ctx: ToolCallContext,
    command: str,
    cd: str | None = None,
    envs: dict[str, str] | None = None,
) -> CommandHandle:
    """
    Start a background command and return its id; same approval/cd/envs rules as `bash`.
    No time limit; stdin is closed. 
    Prefer this over other background execution methods (such as `&` and `nohup`).
    Use `bash_read` for output, `bash_wait` for the result, or `bash_stop` to terminate.
    Output is spooled to disk. The process group is cleaned up on exit or agent
    finalization; jobs cannot be restored after restart.
    """
    cwd = prepare_command(ctx, command, cd)
    ctx.agent.check_cancel()
    pool = CommandPool.of(ctx)
    identifier = uuid.uuid4().hex
    with pool.lock:
        if pool.closed:
            raise RuntimeError("Cannot start a command after its pool has shut down.")
        pool.value[identifier] = CommandJob.start(command, cwd, envs)
    return CommandHandle(id=identifier)


def bash_list(ctx: ToolCallContext) -> list[CommandInfo]:
    """List background commands, including finished jobs awaiting result collection."""
    pool = CommandPool.of(ctx)
    with pool.lock:
        return [
            CommandInfo(id=identifier, command=job.command,
                        status="finished" if job.finished.is_set() else "running")
            for identifier, job in pool.value.items()
        ]


def bash_read(
    ctx: ToolCallContext,
    identifier: str,
    max_output_size: int | None = 16_000,
) -> CommandOutput:
    """
    Read cumulative output, status, exit code and duration without waiting or consuming.
    `max_output_size` limits characters per stream (head/tail plus marker); null returns
    all output. Decode as UTF-8, replacing invalid bytes.
    """
    _validate_output_size(max_output_size)
    return CommandPool.of(ctx).get(identifier).output(identifier, max_output_size)


def bash_wait(
    ctx: ToolCallContext,
    identifier: str,
    timeout: float | None = 300,
    max_output_size: int | None = 16_000,
) -> CmdExecResult | WaitTimeout:
    """
    Collect the final result and remove the job; output limits are as in `bash_read`.
    `timeout` is seconds: 0 polls, null waits indefinitely. Timeout returns
    {"status": "timeout", "id": ..., "notice": ...} without stopping the command.
    Cancellation stops the command but retains its output.
    """
    _validate_timeout(timeout)
    _validate_output_size(max_output_size)
    pool = CommandPool.of(ctx)
    job = pool.get(identifier)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while not job.finished.is_set():
            ctx.agent.check_cancel()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return WaitTimeout(
                    id=identifier, status="timeout",
                    notice="The command is still running; call bash_wait again with this id.",
                )
            job.finished.wait(0.2 if remaining is None else min(0.2, remaining))
        ctx.agent.check_cancel()
    except (CancelledError, KeyboardInterrupt):
        job.stop()
        raise
    return pool.collect(identifier, max_output_size)


def bash_stop(ctx: ToolCallContext, identifier: str) -> CommandInfo:
    """Terminate the command's process group and reap it. Retain output for read/wait."""
    job = CommandPool.of(ctx).get(identifier)
    job.stop()
    return CommandInfo(id=identifier, command=job.command, status="finished")


def expose_cmd_tools() -> list[Callable]:
    import rich
    if os.name == "nt":
        rich.print("[Warning] The bash tools are not available on Windows. Skip registering them.")
        return []
    return [bash, bash_background, bash_wait, bash_read, bash_list, bash_stop]
