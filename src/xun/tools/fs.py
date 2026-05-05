from pathlib import Path
import shutil
from typing import Optional, Literal, Callable
from ..context import execution_context, global_context, tool_call_context
from ..util import fmt_size, fmt_time

def path_check(path: str):
    cwd_abs = Path.cwd().resolve()
    path_abs = Path(path).resolve()
    path_in_cwd = str(path_abs).startswith(str(cwd_abs))
    temp_dirs = global_context.lock().tempdirs
    path_in_temp_dir = any(temp_dir.resolve() in path_abs.parents for temp_dir in temp_dirs)
    if not path_in_cwd and not path_in_temp_dir:
        raise ValueError("Only paths within the current working directory, or any agent's temporary directory are allowed to be accessed.")
    return {
        "path_in_cwd": path_in_cwd,
        "path_in_temp_dir": path_in_temp_dir,
    }

def __path_check_all(*paths: str):
    return [path_check(path) for path in paths]

def __confirm_dangerous_operation(operation: str) -> bool:
    message = f"Going to {operation}."
    tool_context = tool_call_context.get()
    assert tool_context is not None, "Tool call context is required for confirming dangerous file system operations."

    return tool_context.display.get_confirm(
        "Proceed?", message,
        title="File System Operation Confirmation",
        subtitle=f"{tool_context.agent.name} ({tool_context.tool_name})",
        default=True,
    )

def fs_temp_dir() -> str:
    """
    Get the path of the agent's temporary directory.
    This directory is unique for each of the agent and is automatically cleaned up on agent's cleanup.
    """
    ctx = execution_context.get()
    if ctx is None:
        raise RuntimeError("No execution context found. This function can only be used within the execution of an agent.")
    return str(ctx.agent.temp_dir)

def fs_list(path: str, details = False) -> dict[Literal["directories", "files"], list[str]]:
    """
    List the contents of a directory at the specified path.
    Returns a list of file and directory names in the specified directory.
    """
    path_check(path)
    if not details:
        return {
            "directories": [str(p.name) for p in Path(path).iterdir() if p.is_dir()],
            "files": [str(p.name) for p in Path(path).iterdir() if p.is_file()],
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
            "directories": [dir_with_details(p) for p in Path(path).iterdir() if p.is_dir()],
            "files": [file_with_details(p) for p in Path(path).iterdir() if p.is_file()],
        }

def fs_read_file(
    path: str,
    start_line: int = 0,
    end_line: Optional[int] = None,
) -> str:
    """
    Read content from a file at the specified path.
    You can specify the start and end line numbers to read a specific portion of the file. (start_line is inclusive, end_line is exclusive)
    """
    path_check(path)
    lines = Path(path).read_text().splitlines()
    if start_line >= len(lines):
        return ""
    return "\n".join(lines[start_line:end_line])

def fs_write_file(path: str, content: str = "") -> Literal["OK"]:
    """
    Write content to a file at the specified path.
    If the file does not exist, it will be created.
    If the file already exists, its content will be overwritten.
    """
    path_loc = path_check(path)
    if Path(path).exists() and not path_loc["path_in_temp_dir"]:
        if not __confirm_dangerous_operation(f"Overwrite existing file `{path}`"):
            raise RuntimeError(f"Operation cancelled by user, file `{path}` was not overwritten.")
    Path(path).write_text(content)
    return "OK"

def fs_move(src: str, dst: str) -> Literal["OK"]:
    """
    Move (rename) a file or directory from src to dst.
    Basically same as `mv` command in Linux.
        - If dst is an existing directory, src will be moved into dst.
        - If dst is an existing file, it will be overwritten by src.
        - If dst does not exist, src will be renamed to dst.
    Under the hood it uses shutil.move, which can move both files and directories.
    """
    path_loc = __path_check_all(src, dst)
    if not Path(src).exists():
        raise FileNotFoundError("Source file/directory does not exist.")
    # If the source file is in temp dir, we can be more lenient. 
    # Otherwise, we require confirmation for move operation.
    if not path_loc[0]["path_in_temp_dir"] and not __confirm_dangerous_operation(f"Move `{src}` to `{dst}`"):
        raise RuntimeError(f"Operation cancelled by user, `{src}` was not moved to `{dst}`.")
    shutil.move(src, dst)
    return "OK"

def fs_copy(src: str, dst: str) -> Literal["OK"]:
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
    __path_check_all(src, dst)
    if not Path(src).exists():
        raise FileNotFoundError("Source file/directory does not exist.")
    if Path(src).is_file():
        if Path(dst).exists() and Path(dst).is_dir():
            shutil.copy2(src, Path(dst) / Path(src).name)
        else:
            shutil.copy2(src, dst)
    elif Path(src).is_dir():
        if Path(dst).exists() and Path(dst).is_file():
            raise FileExistsError("Destination path exists as a file, cannot copy a directory onto a file.")
        elif Path(dst).exists() and Path(dst).is_dir():
            shutil.copytree(src, Path(dst) / Path(src).name)
        else:
            shutil.copytree(src, dst)
    return "OK"

def fs_mkdir(path: str) -> Literal["OK"]:
    """
    Create a directory at the specified path.
    If the directory already exists, it does nothing.
    """
    path_check(path)
    Path(path).mkdir(exist_ok=True)
    return "OK"

def fs_delete(path: str) -> Literal["OK"]:
    """
    Delete a file or directory at the specified path.
    If the path is a directory, it will be deleted recursively.
    """
    path_loc = path_check(path)
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError("File/directory does not exist.")

    if not path_loc["path_in_temp_dir"] and not __confirm_dangerous_operation(f"Delete `{path}`"):
        raise RuntimeError(f"Operation cancelled by user, `{path}` was not deleted.")

    if p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)
    return "OK"

def fs_request_image(src: str) -> Literal["OK"]:
    """
    You can request an image using the `request_image` tool.

    Call this tool whenever:
    - The request depends on visual details
    - The input is ambiguous without seeing an image
    - The task involves inspecting objects, scenes, diagrams, or UI

    The input can be a single local image path or URL.
    """
    def is_url(path: str) -> bool:
        return path.startswith("http://") or path.startswith("https://")
    if not is_url(src):
        path_check(src)
        if not Path(src).exists():
            raise FileNotFoundError("Source image file does not exist.")
    ctx = tool_call_context.get()
    assert ctx is not None, "Tool call context is required for requesting images."
    ctx.agent.conversation.add_user_message("", images=[src])
    return "OK"

def expose_fs_tools() -> list[Callable]:
    tools = [
        fs_list,
        fs_read_file,
        fs_temp_dir,
        fs_write_file,
        fs_mkdir,
        fs_move,
        fs_copy, 
        fs_delete,
        fs_request_image,
    ]
    return tools