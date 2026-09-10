

import locale
import os
import platform
from datetime import datetime
from typing import Callable, Literal
from ..toolcall import tool_attr, ToolCallContext

def _iana_timezone() -> str:
    """Best-effort IANA timezone name (e.g. 'Asia/Shanghai') resolved from /etc/localtime."""
    try:
        real = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    marker = "zoneinfo/"
    return real.split(marker, 1)[1] if marker in real else ""

def system_info() -> dict:
    """
    Get basic system information.
    Including (but not limited to) operating system, architecture, processor, timezone, locale, and environment variables.
    """
    now = datetime.now().astimezone()
    try:
        language, encoding = locale.getlocale()
    except Exception:
        language = encoding = None
    info = {
        "os": platform.system(),
        "os_version": platform.version(),
        "node_name": platform.node(),
        "release": platform.release(),
        "architecture": platform.machine(),
        "Processor": platform.processor(),
        "timezone": _iana_timezone() or (now.tzname() or ""),
        "utc_offset": now.strftime("%z"),
        "locale": f"{language or ''}.{encoding or ''}".strip("."),
        "locale_env": os.environ.get("LC_ALL") or os.environ.get("LANG") or "",
    }
    return info

@tool_attr(name="datetime")
def system_time() -> str:
    """ Get the current system time and timezone """
    now = datetime.now().astimezone()
    return now.isoformat()

@tool_attr(name="ask_preference")
def system_ask_user_preference(
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
    )
    return {
        "Q": question,
        "A": a,
    }


def expose_system_tools() -> list[Callable]:
    return [system_info, system_time, system_ask_user_preference]