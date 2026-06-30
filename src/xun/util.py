
import os
from datetime import datetime

def fmt_size(size: int | float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f}{unit}"
        size /= 1024
    return f"{size:.2f}PB"

def fmt_time(timestamp: float) -> str:
    dt = datetime.fromtimestamp(timestamp)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def _read_text_if_exists(path: str) -> str:
    try:
        with open(path, "rt", encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except OSError:
        return ""


def parse_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None