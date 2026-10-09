import os
from pathlib import Path
import subprocess
from typing import Callable

from .common import get_policy, resolve_path
from .fs import ask_for_write_permission
from ..toolcall import ToolCallContext


def _run_git_apply(directory: Path, patch: str, *options: str) -> str:
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        env.pop(name, None)
    # Parent repositories must not reinterpret paths relative to their root.
    env["GIT_CEILING_DIRECTORIES"] = str(directory.parent)
    try:
        result = subprocess.run(
            [
                "git", "-c", "apply.ignoreWhitespace=no", "apply", "--no-index",
                "--recount", "--whitespace=nowarn", "-p1", *options, "-",
            ],
            cwd=directory,
            env=env,
            input=patch.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("apply_patch requires Git. Install Git and try again.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "git apply timed out. Inspect the files before retrying."
        ) from exc
    if result.returncode != 0:
        output = "\n".join(
            part.decode("utf-8").strip()
            for part in (result.stdout, result.stderr) if part.strip()
        )
        raise RuntimeError(
            f"git apply failed (exit {result.returncode}):\n{output}\n"
            "Read the current files and regenerate a Git unified diff. "
            "Start each file with 'diff --git a/path b/path'. "
            "Do not use '*** Begin Patch' or Markdown fences."
        )
    return result.stdout.decode("utf-8")


def apply_patch(
    ctx: ToolCallContext,
    patch: str,
    directory: str = ".",
) -> str:
    r"""Modify existing regular text files using a Git unified diff.

    Creation, deletion, rename, copy, binary and mode/type changes are forbidden.
    Neither the patch nor target files may contain NUL bytes.
    Use the filesystem tools for those operations. Existing non-temporary files
    require the same write approval or allowlist grant as write_file.

    Pass raw diff text in the JSON `patch` string, without Markdown fences or
    '*** Begin Patch'. Paths are relative to `directory` (default: workdir).
    Start each file with 'diff --git a/path b/path', including in multi-file
    patches. Always use a/ and b/ prefixes; no absolute paths or '..'.

    Example:
    diff --git a/example.txt b/example.txt
    --- a/example.txt
    +++ b/example.txt
    @@ -1 +1 @@
    -old
    +new

    Read files before editing. Include exact old text and preferably 3 unchanged
    lines around changes, each prefixed with a space. Prefix removed lines with
    '-' and added lines with '+'. Use numeric @@ headers; Git recounts hunk line
    counts. Do not use /dev/null or file-operation/mode headers.
    After a last line without a newline, add '\ No newline at end of file'.

    The Git index is not touched. No fuzzy matching, reverse or partial-apply
    fallback is used. On failure, re-read the files and regenerate the diff.
    Requires the Git executable, but no repository or git init.
    """
    if not patch.strip():
        raise ValueError("Patch content is empty.")
    if "\0" in patch:
        raise ValueError("Patch content must not contain NUL bytes.")
    if not patch.endswith("\n"):
        patch += "\n"
    root = resolve_path(ctx, directory).path.resolve()
    if not root.is_dir():
        raise ValueError(f"Patch directory does not exist or is not a directory: {root}")

    output = _run_git_apply(root, patch, "--numstat", "-z", "--summary", "--check")
    stats, _, summary = output.rpartition("\0")
    entries = [entry.split("\t", 2) for entry in stats.split("\0") if entry]
    if summary or any("-" in entry[:2] for entry in entries):
        raise ValueError(
            "apply_patch only modifies existing text files. "
            "Use filesystem tools for creation, deletion, rename, copy or mode/type changes."
        )
    paths = sorted({entry[2] for entry in entries})
    if not paths:
        raise ValueError("Patch contains no file changes.")
    targets = []
    for path in paths:
        if not (root / path).resolve().is_relative_to(root):
            raise ValueError(f"Patch path is outside the patch directory: {path}")
        target = resolve_path(ctx, root / path)
        if target.path.is_symlink() or not target.path.is_file():
            raise ValueError(f"Patch target must be an existing regular file: {path}")
        with target.path.open("rb") as file:
            if any(b"\0" in chunk for chunk in iter(lambda: file.read(8192), b"")):
                raise ValueError(f"Patch target contains NUL bytes, not text: {path}")
        targets.append(target)

    allowlist = get_policy(ctx).write_allowlist
    for target in targets:
        if not target.in_tempdir and not allowlist.has(target.path):
            if not ask_for_write_permission(
                ctx, target.path, f"Apply patch to existing file `{target.path}`?"
            ):
                raise RuntimeError("Operation cancelled by user. Patch files were not modified.")

    _run_git_apply(root, patch)
    return f"Applied successfully to {len(paths)} file(s): {', '.join(paths)}"


def expose_patch_tools() -> list[Callable]:
    return [apply_patch]
