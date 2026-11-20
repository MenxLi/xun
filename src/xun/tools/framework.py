from typing import Callable, Literal, NotRequired, TypedDict
from ..core.toolcall import tool_attr, ToolCallContext
from ..core.types import ConclusiveToolResult, JsonType
from .common import resolve_path

@tool_attr(name="ask_preference")
def framework_ask_user_preference(
    ctx: ToolCallContext,
    question: str, 
    choices: list[str],
    allow_extra: bool = False,
    default_choice: str | None = None,
    title: str = "User Preference Query",
    ) -> dict[Literal["Q", "A"], str]:
    """
    Query user about their preferences. 
    Use this tool to ask the user to choose from a list of options, and return the selected option.

    - `question`: The question to ask the user explaining the context of the query.
    - `choices`: A list of strings representing the available choices for the user to select from.
    - `allow_extra`: if set to True, the user can have the option to enter their own choice if none of the provided choices are suitable.
    """
    if default_choice is not None and default_choice not in choices:
        raise ValueError(f"Default choice '{default_choice}' is not in the list of available choices.")
    a = ctx.agent.get_choice(
        prompt="Please select your preference",
        choices=choices,
        message=question,
        title=title,
        subtitle="Agent Preference Query",
        default=default_choice,
        allow_extra=allow_extra, 
        skip_auto_confirm=True,
    ).choice
    return {
        "Q": question,
        "A": a,
    }

@tool_attr(name="extract_compacted_tool_result")
def framework_extract_compacted_tool_result(
    ctx: ToolCallContext,
    toolcall_id: str,
) -> str:
    """
    Retrieve the original content of a compacted tool call by its ID.
    Use this only for tool result marked as [Compacted, ID: ...]
    Fails if the ID is unknown or its original content was not kept.
    """
    content = ctx.agent.conversation.compacted_toolcall_result(toolcall_id)
    if content is None:
        raise ValueError(f"No compacted tool call result found for ID '{toolcall_id}'.")
    return content

class ArtifactRef(TypedDict):
    """A workspace artifact produced by the agent: a file or folder, relative to the workdir."""
    path: str
    note: NotRequired[str]

@tool_attr(name="respond_with_artifacts")
def framework_respond_with_artifacts(
    ctx: ToolCallContext,
    content: str,
    artifacts: list[ArtifactRef],
) -> ConclusiveToolResult:
    """
    Deliver the final reply and end the turn.
    Call this as the last step of the task, whenever you need to deliver a reply with file references.

    - `content`: the text reply shown to the user.
    - `artifacts`: files/folders you want to refer to, each with `path` (relative to the working directory) and an optional `note`.
      Whenever your reply involves files you produced, attach them here.

    If you just need to reply without attaching files, just end the turn without tool calls.
    """
    workdir = ctx.agent.workspace.workdir.resolve()
    refs: list[JsonType] = []
    seen: set[str] = set()
    for ref in artifacts:
        resolved = resolve_path(ctx, ref["path"])
        if not resolved.in_workdir:
            raise ValueError(f"'{ref['path']}' is outside the working directory; report artifacts relative to it.")
        path = resolved.path.resolve()  # normalize for relative_to; ResolvedPath.path keeps '..' verbatim
        is_dir = path.is_dir()
        if not (is_dir or path.is_file()):
            raise ValueError(f"'{ref['path']}' does not exist or is not a file/folder.")
        rel = path.relative_to(workdir).as_posix()
        if rel in seen:
            continue
        seen.add(rel)
        refs.append({"path": rel, "dir": is_dir, "note": ref.get("note")})
    return ConclusiveToolResult(content=content, meta={"artifacts": refs})

def expose_framework_tools() -> list[Callable]:
    return [framework_ask_user_preference, framework_extract_compacted_tool_result, framework_respond_with_artifacts]
