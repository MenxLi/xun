"""Blocking commands and agent-owned background bash jobs."""

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
from threading import Event, Lock, Thread
import time
from typing import Callable, Literal, Sequence
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


class SpawnResult(TypedDict):
    id: str
    stdout_log: str
    stderr_log: str


class RunningCommand(TypedDict):
    id: str
    command: str
    stdout_log: str
    stderr_log: str
    uptime: str


class CommandResult(TypedDict):
    stdout_log: str
    stderr_log: str
    returncode: int
    duration: str


class StillRunning(TypedDict):
    status: Literal["running"]
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
    stdout_log: Path
    stderr_log: Path
    started: float
    done: Event = field(default_factory=Event)
    ended: float | None = None
    error: OSError | None = None
    stop_lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    @classmethod
    def start(
        cls, identifier: str, command: str, cwd: Path,
        envs: dict[str, str] | None, log_dir: Path,
    ) -> "CommandJob":
        stdout_log = log_dir / f"bash_{identifier}.stdout.log"
        stderr_log = log_dir / f"bash_{identifier}.stderr.log"
        started = time.monotonic()
        try:
            with open(stdout_log, "wb") as stdout, open(stderr_log, "wb") as stderr:
                process = subprocess.Popen(
                    command, shell=True, executable=shutil.which("bash") or "/bin/sh",
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    cwd=cwd, env=_environment(envs),
                    start_new_session=True,
                )  # nosec B602
        except OSError:
            stdout_log.unlink(missing_ok=True)
            stderr_log.unlink(missing_ok=True)
            raise
        job = cls(command, process, stdout_log, stderr_log, started)
        try:
            Thread(target=job._watch, name=f"bash-{process.pid}", daemon=True).start()
        except RuntimeError:
            job.stop()
            raise
        return job

    def _watch(self) -> None:
        try:
            self.process.wait()
            # Kill surviving descendants even if the shell has already exited.
            _signal_process(self.process, hard=True)
        except OSError as error:
            self.error = error
        finally:
            self.ended = time.monotonic()
            self.done.set()

    def stop(self) -> None:
        with self.stop_lock:
            if self.done.is_set() and self.error is None:
                return
            _signal_process(self.process)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                _signal_process(self.process, hard=True)
            self.done.wait()

    def running_info(self, identifier: str) -> RunningCommand:
        return RunningCommand(
            id=identifier, command=self.command,
            stdout_log=str(self.stdout_log), stderr_log=str(self.stderr_log),
            uptime=_duration(time.monotonic() - self.started),
        )

    def result(self) -> CommandResult:
        if self.error is not None:
            raise RuntimeError(f"Failed to monitor command: {self.command}") from self.error
        return CommandResult(
            stdout_log=str(self.stdout_log), stderr_log=str(self.stderr_log),
            returncode=self.process.returncode,
            duration=_duration((self.ended if self.ended is not None else time.monotonic()) - self.started),
        )


@dataclass
class CommandPool(RuntimeEntry[dict[str, CommandJob]]):
    lock: Lock = field(default_factory=Lock, repr=False, compare=False)
    closed: bool = False
    log_files: set[Path] = field(default_factory=set)

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

    def running(self) -> list[RunningCommand]:
        with self.lock:
            return [job.running_info(identifier)
                    for identifier, job in self.value.items() if not job.done.is_set()]

    def remove(self, identifier: str) -> None:
        with self.lock:
            self.value.pop(identifier, None)

    def shutdown(self) -> None:
        with self.lock:
            self.closed = True
            jobs = list(self.value.values())
            self.value.clear()
            logs, self.log_files = self.log_files, set()
        for job in jobs:
            job.stop()
        for log in logs:
            log.unlink(missing_ok=True)


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
    null disables truncation. For non-blocking execution, use `bash_spawn`.
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


def bash_spawn(
    ctx: ToolCallContext,
    command: str,
    cd: str | None = None,
    envs: dict[str, str] | None = None,
) -> SpawnResult:
    """
    Start a background command and return its id and log file paths; same
    approval/cd/envs rules as `bash`. No time limit; stdin is closed.
    Prefer this over other background execution methods (such as `&` and `nohup`).

    Stdout/stderr stream in real time to `bash_<id>.stdout.log` /
    `bash_<id>.stderr.log` under the workspace temp directory. 

    `bash_kill` to terminate, `bash_running` to list running commands; a finished
    command is only visible to `bash_wait` until its result is returned.
    On agent finalization, running commands are killed and all logs are deleted.
    """
    cwd = prepare_command(ctx, command, cd)
    ctx.agent.check_cancel()
    pool = CommandPool.of(ctx)
    identifier = uuid.uuid4().hex
    with pool.lock:
        if pool.closed:
            raise RuntimeError("Cannot start a command after its pool has shut down.")
        job = CommandJob.start(identifier, command, cwd, envs, ctx.agent.workspace.tempdir.path)
        pool.value[identifier] = job
        pool.log_files.update((job.stdout_log, job.stderr_log))
    return SpawnResult(id=identifier, stdout_log=str(job.stdout_log), stderr_log=str(job.stderr_log))


def bash_running(ctx: ToolCallContext) -> list[RunningCommand]:
    """List the background commands currently running, with id, command, log paths and uptime."""
    return CommandPool.of(ctx).running()


def bash_wait(
    ctx: ToolCallContext,
    identifier: str,
    timeout: float | None = 600,
) -> CommandResult | StillRunning:
    """
    Wait for the command to exit and return its exit code, duration and log paths,
    forgetting the command (the logs stay until agent finalization). Read them for output.

    `timeout` is seconds: 0 polls, null waits indefinitely.
    While still running, return {"status": "running", ...} without stopping the command.
    """
    _validate_timeout(timeout)
    pool = CommandPool.of(ctx)
    job = pool.get(identifier)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while not job.done.is_set():
            ctx.agent.check_cancel()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return StillRunning(
                    status="running",
                    notice="The command is still running; call bash_wait again with this id.",
                )
            job.done.wait(0.2 if remaining is None else min(0.2, remaining))
        ctx.agent.check_cancel()
    except (CancelledError, KeyboardInterrupt):
        job.stop()
        raise
    result = job.result()
    pool.remove(identifier)
    return result


def bash_kill(ctx: ToolCallContext, identifier: str) -> CommandResult:
    """Terminate the command's process group and return the same result as `bash_wait`,
    forgetting the command (the logs stay until agent finalization)."""
    pool = CommandPool.of(ctx)
    job = pool.get(identifier)
    job.stop()
    result = job.result()
    pool.remove(identifier)
    return result


def expose_cmd_tools() -> list[Callable]:
    import rich
    if os.name == "nt":
        rich.print("[Warning] The bash tools are not available on Windows. Skip registering them.")
        return []
    return [bash, bash_spawn, bash_wait, bash_running, bash_kill]
