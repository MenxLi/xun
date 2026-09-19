import json
import py_compile
import shutil
import subprocess
import difflib
from typing import Callable, Literal
from ..toolcall import ToolCallContext
from .common import resolve_path

LANGUAGE = Literal["python", "json", "bash"]


def check_syntax(
    ctx: ToolCallContext,
    path: str,
    language: LANGUAGE,
) -> dict[Literal["valid", "error_msg"], bool | str]:
    """
    Check the syntax of a file by parsing it.
    Supports python, json, and bash.
    Does not execute the file — only validates its syntax.
    """
    resolved = resolve_path(ctx, path).path

    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"Path is not a file: {resolved}")

    match language:
        case "python":
            return _check_python(resolved)
        case "json":
            return _check_json(resolved)
        case "bash":
            return _check_bash(resolved)


def _check_python(path) -> dict:
    try:
        py_compile.compile(str(path), doraise=True)
        return {"valid": True, "error_msg": ""}
    except py_compile.PyCompileError as e:
        return {"valid": False, "error_msg": str(e)}


def _check_json(path) -> dict:
    try:
        json.loads(path.read_text())
        return {"valid": True, "error_msg": ""}
    except json.JSONDecodeError as e:
        return {"valid": False, "error_msg": f"Line {e.lineno}, col {e.colno}: {e.msg}"}


def _check_bash(path) -> dict:
    bash = shutil.which("bash")
    if bash is None:
        raise EnvironmentError("bash is not available on this system")
    result = subprocess.run(
        [bash, "-n", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        return {"valid": True, "error_msg": ""}
    return {"valid": False, "error_msg": (result.stderr or result.stdout).strip()}


def diff_files(
    ctx: ToolCallContext,
    path_a: str,
    path_b: str,
) -> str:
    """
    Show a unified diff between two files.
    Useful for inspecting what changed between versions of a file.
    Returns an empty string if the files are identical.
    """
    ra = resolve_path(ctx, path_a)
    rb = resolve_path(ctx, path_b)

    if not ra.path.exists():
        raise FileNotFoundError(f"File not found: {ra.path}")
    if not rb.path.exists():
        raise FileNotFoundError(f"File not found: {rb.path}")

    lines_a = ra.path.read_text().splitlines(keepends=True)
    lines_b = rb.path.read_text().splitlines(keepends=True)

    diff = difflib.unified_diff(
        lines_a, lines_b,
        fromfile=str(ra.path.name),
        tofile=str(rb.path.name),
    )
    return "".join(diff)


def check_lint(
    ctx: ToolCallContext,
    path: str,
    language: Literal["python"] = "python",
) -> dict[Literal["valid", "error_msg"], bool | str]:
    """
    Run mypy linting on a Python file.
    If mypy is not installed, prompts for confirmation to install it via pip.
    """
    resolved = resolve_path(ctx, path).path

    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"Path is not a file: {resolved}")

    mypy = shutil.which("mypy")
    if mypy is None:
        # Mypy not installed - ask user
        confirm = ctx.agent.get_confirm(
            "Install mypy?",
            "mypy is not installed on this system. Would you like to install it via pip for linting?",
            title="Mypy Installation",
            subtitle=f"{ctx.agent.name} ({ctx.tool_name})",
            default=True,
        )
        if confirm.choice:
            try:
                result = subprocess.run(
                    [shutil.which("pip") or "pip", "install", "mypy"],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    return {"valid": True, "error_msg": "mypy installed successfully"}
                else:
                    return {"valid": False, "error_msg": f" Installation failed: {result.stderr}"}
            except Exception as e:
                return {"valid": False, "error_msg": f"Could not install mypy: {e}"}
        else:
            return {"valid": False, "error_msg": "Linting cancelled by user"}

    # Mypy is available - run it
    result = subprocess.run(
        [mypy, "--no-color-output", str(resolved)],
        capture_output=True, text=True, timeout=120,
    )
    # mypy return code: 0 = no errors, 1 = errors, 2 = system/reject error
    if result.returncode == 0:
        return {"valid": True, "error_msg": ""}
    # Normalize output
    output = result.stderr or result.stdout
    return {"valid": False, "error_msg": output.strip()}


def expose_diagnostic_tools() -> list[Callable]:
    return [check_syntax, diff_files, check_lint]