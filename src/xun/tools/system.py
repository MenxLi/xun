

import locale
import os
import platform
from datetime import datetime
from typing import Callable
from ..toolcall import tool_attr

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

def expose_system_tools() -> list[Callable]:
    return [system_info, system_time]
