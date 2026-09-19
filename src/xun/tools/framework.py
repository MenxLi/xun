from typing import Callable, Literal
from ..toolcall import tool_attr, ToolCallContext

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
        _skip_auto_confirm=True,
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

def expose_framework_tools() -> list[Callable]:
    return [framework_ask_user_preference, framework_extract_compacted_tool_result]
