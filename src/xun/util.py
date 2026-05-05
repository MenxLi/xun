
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

def is_in_container() -> bool:
    """
    Best-effort detection for common container runtimes.

    This remains heuristic. Callers that need a source of truth should use an
    explicit environment variable override.
    """
    forced = parse_bool("XUN_IN_CONTAINER")
    if forced is not None:
        return forced

    runtime_hint = parse_bool("container")
    if runtime_hint is not None:
        return runtime_hint

    marker_paths = (
        "/.dockerenv",
        "/.containerenv",
        "/run/.containerenv",
        "/run/systemd/container",
    )
    if any(os.path.exists(path) for path in marker_paths):
        return True

    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True

    proc_indicators = (
        "docker",
        "containerd",
        "kubepods",
        "podman",
        "libpod",
        "lxc",
    )
    for proc_file in (
        "/proc/1/cgroup",
        "/proc/self/cgroup",
        "/proc/1/mountinfo",
        "/proc/self/mountinfo",
    ):
        content = _read_text_if_exists(proc_file).lower()
        if any(indicator in content for indicator in proc_indicators):
            return True

    return False