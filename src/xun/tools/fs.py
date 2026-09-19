import os
import re
from pathlib import Path
import shutil
from typing import Optional, Literal, Callable

from ..toolcall import ToolCallContext as Context
from ..toolcall import tool_attr
from ..util import fmt_size, fmt_time
from .common import (
    defer_tool_image,
    resolve_path, 
    is_path_binary, 
    glob_match,
    get_policy, 
    git_ignored_paths,
)

def ask_for_write_permission(ctx: Context, path: Path, message: str) -> bool:
    """
    Ask the user for permission to write to a file or directory.
    Returns True if the user confirms, False otherwise.
    """
    GRANT_WRITE_PERMISSION = "Allow, and grant this agent to write to this file/directory."
    outcome = ctx.agent.get_choice(
        "Confirm Write Permission",
        choices = [
            GRANT_WRITE_PERMISSION, 
            "Allow once", 
            "Deny",
        ],
        message=message,
        title="Write Permission Request",
        subtitle=f"{ctx.agent.name} ({ctx.tool_name})",
        default=GRANT_WRITE_PERMISSION
    )
    # Only a user pick may add a persistent allowlist entry.
    if outcome.choice == GRANT_WRITE_PERMISSION and outcome.source == "user":
        get_policy(ctx).write_allowlist.add(path, is_dir=path.is_dir())
        return True
    return outcome.choice != "Deny"

@tool_attr(name="temp_dir")
def fs_temp_dir(ctx: Context) -> str:
    """
    Get the path of the agent's temporary directory.
    This directory is unique for each of the agent.

    **Prefer this over using the system's temp directory, **
    if you need to save files temporarily.
    """
    return str(ctx.agent.workspace.tempdir.path)

@tool_attr(name="list_dir")
def fs_list(ctx: Context, path: str, details = False) -> dict[Literal["directories", "files"], list[str]]:
    """
    List the contents of a directory at the specified path.
    Returns a list of file and directory names in the specified directory.
    """
    rpath = resolve_path(ctx, path).path
    if not details:
        return {
            "directories": [str(p.name) for p in rpath.iterdir() if p.is_dir()],
            "files": [str(p.name) for p in rpath.iterdir() if p.is_file()],
        }
    else:
        def file_with_details(p: Path) -> str:
            stat = p.stat()
            return f"{p.name} [{fmt_size(stat.st_size)}, modified: {fmt_time(stat.st_mtime)}, created: {fmt_time(stat.st_ctime)}, mode: {oct(stat.st_mode)}]"
        def dir_with_details(p: Path) -> str:
            stat = p.stat()
            n_content = len(list(p.iterdir()))
            return f"{p.name}/ [{n_content} items, created: {fmt_time(stat.st_ctime)}, mode: {oct(stat.st_mode)}]"

        return {
            "directories": [dir_with_details(p) for p in rpath.iterdir() if p.is_dir()],
            "files": [file_with_details(p) for p in rpath.iterdir() if p.is_file()],
        }

@tool_attr(name="file_info")
def fs_file_info(ctx: Context, path: str) -> dict:
    """
    Get metadata about a file without reading its content.
    Returns size, line count, last modified time, and whether it appears to be text or binary.
    """
    resolved = resolve_path(ctx, path).path
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"Path is not a file: {resolved}")

    stat = resolved.stat()
    size = stat.st_size
    is_text = True
    line_count = 0

    try:
        with resolved.open("rb") as file:
            first_chunk = file.read(8192)
            is_text = b"\x00" not in first_chunk
            if not is_text:
                line_count = 0
            else:
                line_count = first_chunk.count(b"\n")
                has_data = bool(first_chunk)
                last_byte = first_chunk[-1:] if first_chunk else b""

                for chunk in iter(lambda: file.read(8192), b""):
                    has_data = True
                    line_count += chunk.count(b"\n")
                    last_byte = chunk[-1:]

                if has_data and last_byte != b"\n":
                    line_count += 1
    except OSError:
        pass

    return {
        "path": str(resolved),
        "size": f"{fmt_size(size)} ({size} bytes)",
        "line_count": line_count,
        "is_text": is_text,
        "last_modified": fmt_time(stat.st_mtime),
    }

@tool_attr(name="read_file")
def fs_read_file(
    ctx: Context,
    path: str,
    line_offset: int = 0,
    line_limit: Optional[int] = None,
    include_line_numbers: bool = False
) -> str:
    """
    Read content from a file at the specified path.
    You can specify the starting line and ending line to read a specific range of lines from the file.
    - line_offset: The number of lines to skip from the start of the file (default is 0).
    - line_limit: The maximum number of lines to read (default is None, which means read all lines from the offset).
    - include_line_numbers: Whether to include line numbers in the output (default is False). \
        If set to True, each line will be prefixed with its line number (e.g., "1: line content"). \
        The line numbers will be based on the original file, not the offset.
    """
    rpath = resolve_path(ctx, path).path
    lines = rpath.read_text().splitlines()
    if line_offset >= len(lines):
        return ""
    end_line = line_offset + line_limit if line_limit is not None else None
    if include_line_numbers:
        return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[line_offset:end_line], start=line_offset))
    else:
        return "\n".join(lines[line_offset:end_line])

@tool_attr(name="write_file")
def fs_write_file(ctx: Context, path: str, content: str = "") -> Literal["OK"]:
    """
    Write content to a file at the specified path.
    If the file does not exist, it will be created.
    If the file already exists, its content will be overwritten.
    """
    resolved = resolve_path(ctx, path)
    if resolved.path.exists() and not resolved.in_tempdir:
        if not get_policy(ctx).write_allowlist.has(resolved.path) and \
            not ask_for_write_permission(ctx, resolved.path, f"File `{resolved.path}` already exists. Do you want to overwrite it?"):
            raise RuntimeError(f"Operation cancelled by user, file `{resolved.path}` was not overwritten.")
    resolved.path.write_text(content)
    return "OK"

@tool_attr(name="move")
def fs_move(ctx: Context, src: str, dst: str) -> Literal["OK"]:
    """
    Move (rename) a file or directory from src to dst.
    Basically same as `mv` command in Linux.
        - If dst is an existing directory, src will be moved into dst.
        - If dst is an existing file, it will be overwritten by src.
        - If dst does not exist, src will be renamed to dst.
    Under the hood it uses shutil.move, which can move both files and directories.
    """
    src_resolved = resolve_path(ctx, src)
    dst_resolved = resolve_path(ctx, dst)
    if not src_resolved.path.exists():
        raise FileNotFoundError("Source file/directory does not exist.")
    # If the source file is in temp dir, we can be more lenient.
    # Otherwise, we require confirmation for move operation.
    # (Move from source equals to delete source and create destination, which is a potentially dangerous.)
    if not src_resolved.in_tempdir: 
        if not get_policy(ctx).write_allowlist.has(src_resolved.path) and \
            not ask_for_write_permission(ctx, src_resolved.path, f"Are you sure you want to move `{src_resolved.path}` to `{dst_resolved.path}`?"):
            raise RuntimeError(f"Operation cancelled by user, `{src_resolved.path}` was not moved to `{dst_resolved.path}`.")
    shutil.move(src_resolved.path, dst_resolved.path)
    return "OK"

@tool_attr(name="copy")
def fs_copy(ctx: Context, src: str, dst: str) -> Literal["OK"]:
    """
    Copy a file or directory from src to dst.
    Basically same as `cp` command in Linux.
        - If src is a file:
            - If dst is an existing directory, src will be copied into dst.
            - If dst is an existing file, it will be overwritten by src.
            - If dst does not exist, src will be copied to dst.
        - If src is a directory:
            - If dst is an existing directory, src will be copied into dst (i.e. dst/src).
            - If dst does not exist, src will be copied to dst (i.e. dst will be created as a copy of src).
            - If dst is an existing file, an error will be raised.
    Under the hood it uses shutil.copy2 for files and shutil.copytree for directories.
    """
    src_resolved = resolve_path(ctx, src)
    dst_resolved = resolve_path(ctx, dst)
    if not src_resolved.path.exists():
        raise FileNotFoundError("Source file/directory does not exist.")
    if src_resolved.path.is_dir() and dst_resolved.path.is_file():
        raise FileExistsError("Destination path exists as a file, cannot copy a directory onto a file.")
    effective_dst = dst_resolved.path / src_resolved.path.name if dst_resolved.path.is_dir() else dst_resolved.path
    # If the destination is in temp dir, we can be more lenient.
    # Otherwise, copying onto an existing destination overwrites it, requiring explicit permission.
    if effective_dst.exists() and not dst_resolved.in_tempdir:
        if not get_policy(ctx).write_allowlist.has(effective_dst) and \
            not ask_for_write_permission(ctx, effective_dst, f"Destination `{effective_dst}` already exists. Are you sure you want to copy `{src_resolved.path}` onto it?"):
            raise RuntimeError(f"Operation rejected by user, `{src_resolved.path}` was not copied to `{effective_dst}` (existing destination).")
    if src_resolved.path.is_file():
        shutil.copy2(src_resolved.path, effective_dst)
    elif src_resolved.path.is_dir():
        shutil.copytree(src_resolved.path, effective_dst)
    return "OK"

@tool_attr(name="mkdir")
def fs_mkdir(ctx: Context, path: str) -> Literal["OK"]:
    """
    Create a directory at the specified path.
    If the directory already exists, it does nothing.
    """
    rpath = resolve_path(ctx, path).path
    rpath.mkdir(exist_ok=True)
    return "OK"

@tool_attr(name="delete")
def fs_delete(ctx: Context, path: str) -> Literal["OK"]:
    """
    Delete a file or directory at the specified path.
    If the path is a directory, it will be deleted recursively.
    """
    resolved = resolve_path(ctx, path)
    p = resolved.path
    if not p.exists():
        raise FileNotFoundError("File/directory does not exist.")

    if not resolved.in_tempdir:
        if not get_policy(ctx).write_allowlist.has(resolved.path) and \
            not ask_for_write_permission(ctx, resolved.path, f"Are you sure you want to delete `{resolved.path}`?"):
            raise RuntimeError(f"Operation cancelled by user, `{resolved.path}` was not deleted.")

    if p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)
    return "OK"

@tool_attr(name="request_image", required_capabilities=["vision"])
def fs_request_image(ctx: Context, src: str) -> Literal["OK"]:
    """
    You can request an image using the `request_image` tool.
    The input can be a single local image path or URL.

    The image will be added to the conversation as a user message with an empty text content.

    Call this tool whenever:
    - The request depends on visual details
    - The input is ambiguous without seeing an image
    - The task involves inspecting objects, scenes, diagrams, or UI
    """
    def is_url(path: str) -> bool:
        return path.startswith("http://") or path.startswith("https://")
    if not is_url(src):
        src_resolved = resolve_path(ctx, src)
        if not src_resolved.path.exists():
            raise FileNotFoundError("Source image file does not exist.")
        src = str(src_resolved.path)
    defer_tool_image(ctx, src)
    return "OK"

@tool_attr(name="glob")
def fs_glob_files(
    ctx: Context,
    path: str = ".",
    name_pattern: str = "*",
    file_type: Literal["file", "directory", "any"] = "any",
    skip_ignored: bool = True,
) -> list[str]:
    """
    Find files or directories by name pattern under the given path.
    Searches recursively. Uses glob-style patterns (e.g., "*.py", "test_*").
    - path: directory to search (default "." - current workdir)
    - name_pattern: glob pattern for file/dir names (default "*")
    - file_type: filter by "file", "directory", or "any" (default "any")
    - skip_ignored: skip files/directories ignored by .gitignore (default True; no effect outside a git repo)
    Returns a sorted list of relative paths (relative to the search root).
    """
    # default to current directory if path is empty or None
    if not path or path.strip() == "":
        path = "."
    rpath = resolve_path(ctx, path).path
    if not rpath.exists():
        raise FileNotFoundError(f"Directory not found: {rpath}")

    matches: list[tuple[str, Path]] = []  # (relative display path, absolute entry path)
    for root, dirs, files in os.walk(rpath):
        root_path = Path(root)
        entries: list[Path] = []
        if file_type in ("file", "any"):
            entries.extend(root_path / f for f in files)
        if file_type in ("directory", "any"):
            entries.extend(root_path / d for d in dirs)

        for entry in entries:
            if entry.name == name_pattern or glob_match(name_pattern, entry.name):
                try:
                    rel = str(entry.relative_to(rpath))
                except ValueError:
                    rel = str(entry)
                matches.append((rel, entry))

    # drop git-ignored matches in one batched query (relative to the repo root)
    if skip_ignored and matches:
        workdir = ctx.agent.workspace.workdir.resolve()
        rels = []
        for _, entry in matches:
            try:
                rels.append(str(entry.resolve().relative_to(workdir)))
            except ValueError:
                rels.append(None)  # outside the repo; not subject to gitignore
        ignored = git_ignored_paths(workdir, [r for r in rels if r is not None])
        matches = [(rel, e) for (rel, e), r in zip(matches, rels) if r is None or r not in ignored]

    matches.sort(key=lambda x: x[0])
    return [rel for rel, _ in matches]


@tool_attr(name="grep")
def fs_grep_files(
    ctx: Context,
    path: str,
    pattern: str,
    file_pattern: str = "*",
    include_content: bool = True,
    regex: bool = True,
    skip_ignored: bool = True,
) -> list[dict]:
    """
    Search for patterns in file contents under the given path.
    Searches recursively, skips binary files.
    - path: directory to search (or a single file)
    - pattern: pattern to search for
    - file_pattern: glob pattern to filter which files to search (default "*")
    - include_content: whether to return matching lines (default True)
    - regex: if True, treat pattern as regex (default True); if False, treat as literal substring
    - skip_ignored: skip files ignored by .gitignore (default True; no effect outside a git repo)
    Returns a list of match entries, each with path, line_number, and content (if enabled).
    """
    rpath = resolve_path(ctx, path).path
    if not rpath.exists():
        raise FileNotFoundError(f"Path not found: {rpath}")

    # compile pattern based on mode
    if regex:
        try:
            compiled_pattern = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern '{pattern}': {e}")
    else:
        compiled_pattern = re.compile(re.escape(pattern))

    results = []
    files_to_search: list[Path] = []

    if rpath.is_file():
        files_to_search = [rpath]
    else:
        workdir = ctx.agent.workspace.workdir.resolve()
        candidate_files: list[Path] = []
        for root, dirs, files in os.walk(rpath):
            # prune git-ignored subdirectories early to avoid descending into them
            if skip_ignored:
                dir_rels: dict[str, Optional[str]] = {d: None for d in dirs}
                for d in dirs:
                    try:
                        dir_rels[d] = str((Path(root) / d).resolve().relative_to(workdir))
                    except ValueError:
                        pass
                ignored = git_ignored_paths(workdir, [r for r in dir_rels.values() if r is not None])
                dirs[:] = [d for d in dirs if dir_rels[d] is None or dir_rels[d] not in ignored]
            for f in files:
                if glob_match(file_pattern, f):
                    candidate_files.append(Path(root) / f)
        if skip_ignored and candidate_files:
            rels: dict[Path, Optional[str]] = {}
            for fp in candidate_files:
                try:
                    rels[fp] = str(fp.resolve().relative_to(workdir))
                except ValueError:
                    rels[fp] = None  # outside the repo; not subject to gitignore
            ignored = git_ignored_paths(workdir, [r for r in rels.values() if r is not None])
            files_to_search = [fp for fp, r in rels.items() if r is None or r not in ignored]
        else:
            files_to_search = candidate_files

    for filepath in files_to_search:
        if is_path_binary(filepath):
            continue
        try:
            with filepath.open("r", errors="ignore") as fh:
                for line_num, line in enumerate(fh, 1):
                    if compiled_pattern.search(line):
                        rel = str(filepath.relative_to(rpath)) if rpath.is_dir() else filepath.name
                        entry = {"path": rel, "line_number": line_num}
                        if include_content:
                            entry["content"] = line.rstrip()
                        results.append(entry)
                        if len(results) >= 100:
                            break
        except (OSError, UnicodeDecodeError):
            continue
        if len(results) >= 100:
            break

    return results


def expose_fs_tools() -> list[Callable]:
    tools = [
        fs_list,
        fs_read_file,
        fs_file_info,
        fs_temp_dir,
        fs_write_file,
        fs_mkdir,
        fs_move,
        fs_copy,
        fs_delete,
        fs_request_image,
        fs_glob_files,
        fs_grep_files,
    ]
    return tools