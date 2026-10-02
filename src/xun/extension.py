"""
Global extension system: drop Python files under `{XUN_HOME}/extensions/` and
every agent picks up their effects at initialization.

Two source forms, each yielding an extension named `{name}`:
- package form: `extensions/{name}/setup_extension.py` (supports relative imports)
- flat form:    `extensions/{name}.py` (zero ceremony; no relative imports)
"""
from __future__ import annotations
import importlib.util
import inspect
import re
import sys
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Callable, cast
import rich
from .config import get_home_dir, xun_version
from .types import CancelledError, JsonType, Result
if TYPE_CHECKING:
    from .agent import Agent

ENTRY_MODULE = "setup_extension"
ENTRY_FILE = f"{ENTRY_MODULE}.py"

class ExtensionStatus(str, Enum):
    """str-Enum so pydantic/JSON treat it as a string."""
    UNINITIALIZED = 'uninitialized'
    """Setup has not run (yet), or was interrupted."""
    LOADED = 'loaded'
    SKIPPED = 'skipped'
    """Not applicable to the running xun version."""
    FAILED = 'failed'

@dataclass(frozen=True)
class ExtensionIssue:
    """Why an extension is unusable: FAILED to import, or SKIPPED by the version gate."""
    name: str
    path: Path
    status: ExtensionStatus
    reason: str

@dataclass(frozen=True)
class ExtensionInfo:
    """Presentation snapshot of one discovered extension, usable or not."""
    name: str
    description: str
    path: Path
    status: ExtensionStatus
    reason: str | None = None
    """Why FAILED or SKIPPED."""

    @classmethod
    def from_issue(cls, issue: ExtensionIssue) -> "ExtensionInfo":
        return cls(name=issue.name, description="", path=issue.path,
                   status=issue.status, reason=issue.reason)

def _version_key(version: str) -> tuple[int, ...]:
    """Numeric core of a version, e.g. '1.2rc3' -> (1, 2)."""
    core = re.match(r"\d+(?:\.\d+)*", version)
    return tuple(int(p) for p in core.group().split(".")) if core else ()

def _compare_versions(a: str, b: str) -> int:
    """Compare version strings on their numeric cores; shorter side zero-padded."""
    ka, kb = _version_key(a), _version_key(b)
    width = max(len(ka), len(kb))
    ka += (0,) * (width - len(ka))
    kb += (0,) * (width - len(kb))
    return (ka > kb) - (ka < kb)

@dataclass(frozen=True)
class ExtensionAttr:
    """A xun API version range declared by an extension, both bounds inclusive."""
    api_min_version: str | None
    api_max_version: str | None

    def attach_to[F: Callable](self, fn: F) -> F:
        """Attach this ExtensionAttr to a function."""
        setattr(fn, "__xun_extension_attr", self)
        return fn

    @classmethod
    def extract_from(cls, fn: Callable) -> "ExtensionAttr | None":
        """Extract an ExtensionAttr from a function, if it exists."""
        return getattr(fn, "__xun_extension_attr", None)

def extension_attr(api_min_version: str | None = None, api_max_version: str | None = None):
    """Declare which xun API versions an extension supports, on its `setup_extension`.
    Outside the range the extension is SKIPPED, not FAILED."""
    def _wrapper[F: Callable](fn: F) -> F:
        return ExtensionAttr(api_min_version=api_min_version, api_max_version=api_max_version).attach_to(fn)
    return _wrapper

@dataclass(frozen=True)
class Extension:
    name: str
    description: str
    setup: Callable[["ExtensionContext"], None]
    path: Path
    api_min_version: str | None = None
    api_max_version: str | None = None

    def version_conflict(self) -> str | None:
        """Why the running xun is outside the declared range, or None when it fits.
        Without version metadata (source checkout) the check passes."""
        current = xun_version()
        if current is None:
            return None
        violations = []
        if self.api_min_version and _compare_versions(current, self.api_min_version) < 0:
            violations.append(f">= {self.api_min_version}")
        if self.api_max_version and _compare_versions(current, self.api_max_version) > 0:
            violations.append(f"<= {self.api_max_version}")
        if not violations:
            return None
        return f"requires xun {', '.join(violations)}, running {current}"

@dataclass(frozen=True)
class ExtensionContext:
    """Passed to `setup_extension`; fresh per `initialize()`."""
    _ext: Extension
    agent: "Agent[Agent.T.Uninit]"

    @property
    def name(self) -> str:
        return self._ext.name

    @property
    def settings(self) -> dict[str, JsonType]:
        """`config.extension_settings[name]`, empty when unset."""
        return self.agent.config.extension_settings.get(self.name, {})

type ScanItem = Result[Extension, ExtensionIssue]

def _warn(msg: str) -> None:
    rich.print(f"[bold yellow]Extension warning:[/bold yellow] {msg}")

def _import_ext_module(name: str, location: Path) -> ModuleType:
    """Import one extension source. Its sys.modules entry stays on success
    (sub-agent replay relies on it), removed before re-raising."""
    pkg_name = f"xun_ext_{name}"
    inserted: list[str] = []
    try:
        if location.is_dir():
            pkg = ModuleType(pkg_name)
            pkg.__path__ = [str(location)]
            sys.modules[pkg_name] = pkg
            inserted.append(pkg_name)
            module_name = f"{pkg_name}.{ENTRY_MODULE}"
            file = location / ENTRY_FILE
        else:
            module_name = pkg_name
            file = location
        spec = importlib.util.spec_from_file_location(module_name, file)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot build an import spec for {file}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        inserted.append(module_name)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        for key in inserted:
            for k in [k for k in sys.modules if k == key or k.startswith(f"{key}.")]:
                del sys.modules[k]
        raise

def _distill(name: str, location: Path) -> ScanItem:
    """Import one extension and extract its metadata."""
    path = location if location.is_file() else location / ENTRY_FILE
    def issue(status: ExtensionStatus, reason: str) -> ScanItem:
        return Result.Err(ExtensionIssue(name=name, path=path, status=status, reason=reason))
    try:
        mod = _import_ext_module(name, location)
        setup = getattr(mod, ENTRY_MODULE, None)
        if not callable(setup):
            return issue(ExtensionStatus.FAILED, f"has no callable '{ENTRY_MODULE}()'")
        attr = ExtensionAttr.extract_from(setup)
        doc = inspect.getdoc(mod) or ""
        ext = Extension(
            name = name,
            description = doc.splitlines()[0] if doc else "",
            setup = cast("Callable[[ExtensionContext], None]", setup),
            path = path,
            api_min_version = attr.api_min_version if attr else None,
            api_max_version = attr.api_max_version if attr else None,
        )
        conflict = ext.version_conflict()
        if conflict:
            return issue(ExtensionStatus.SKIPPED, conflict)
        return Result.Ok(ext)
    except (KeyboardInterrupt, CancelledError):
        raise
    except Exception as e:
        return issue(ExtensionStatus.FAILED, f"failed to import: {e}")

@dataclass(frozen=True)
class _SetupOutcome:
    """How the latest setup_extension run ended for an extension."""
    status: ExtensionStatus
    reason: str | None = None

_UNINITIALIZED_OUTCOME = _SetupOutcome(ExtensionStatus.UNINITIALIZED)

class ExtensionLoader:
    """Discovers, imports and applies the extensions of one dir.
    `default_loader` below serves `{XUN_HOME}` process-wide."""

    def __init__(self, get_extension_dir: Callable[[], Path] = lambda: get_home_dir() / "extensions") -> None:
        self._get_extension_dir = get_extension_dir
        self._scan_lock = threading.Lock()
        self._scans: dict[Path, tuple[ScanItem, ...]] = {}
        self._status_lock = threading.Lock()
        # scan results are cached and frozen, so setup outcomes live here
        self._setup_outcomes: dict[Path, _SetupOutcome] = {}

    def scan(self) -> tuple[ScanItem, ...]:
        """One Result per candidate, sorted by name; cached per dir."""
        with self._scan_lock:
            ext_dir = self._get_extension_dir()
            if ext_dir not in self._scans:
                self._scans[ext_dir] = self._import_all(ext_dir)
            return self._scans[ext_dir]

    def clear_scan_cache(self) -> None:
        """Force the next scan to re-import; outcomes are kept."""
        with self._scan_lock:
            self._scans.clear()

    def imported(self) -> list[Extension]:
        """Extensions that imported successfully."""
        return [item.unwrap() for item in self.scan() if item.is_ok()]

    def infos(self) -> list[ExtensionInfo]:
        """Every discovered extension with its current status."""
        infos: list[ExtensionInfo] = []
        for item in self.scan():
            if item.is_err():
                infos.append(ExtensionInfo.from_issue(item.unwrap_err()))
                continue
            ext = item.unwrap()
            with self._status_lock:
                outcome = self._setup_outcomes.get(ext.path, _UNINITIALIZED_OUTCOME)
            infos.append(ExtensionInfo(
                name = ext.name, 
                description = ext.description, 
                path = ext.path, 
                status = outcome.status, 
                reason = outcome.reason
                ))
        return infos

    def apply(self, agent: "Agent[Agent.T.Uninit]") -> None:
        """Run each extension's setup on an uninitialized agent;
        failures warn and never block startup."""
        if not agent.config.enable_extensions:
            return
        for item in self.scan():
            if item.is_err():
                continue  # already warned during the scan
            ext = item.unwrap()
            try:
                ext.setup(ExtensionContext(_ext=ext, agent=agent))
            except (KeyboardInterrupt, CancelledError):
                raise  # an interrupt is not a failure
            except Exception as e:
                _warn(f"extension '{ext.name}' setup failed (agent startup continues): {e}")
                self._record(ext.path, ExtensionStatus.FAILED, str(e))
            else:
                self._record(ext.path, ExtensionStatus.LOADED)

    def _record(self, path: Path, status: ExtensionStatus, reason: str | None = None) -> None:
        with self._status_lock:
            self._setup_outcomes[path] = _SetupOutcome(status, reason)

    def _import_all(self, ext_root: Path) -> tuple[ScanItem, ...]:
        if not ext_root.is_dir():
            return ()
        packages: dict[str, Path] = {}
        flats: dict[str, Path] = {}
        for loc in sorted(ext_root.iterdir()):
            if loc.name.startswith((".", "__")):
                continue
            if loc.is_dir():
                if (loc / ENTRY_FILE).is_file():
                    packages[loc.name] = loc
                else:
                    _warn(f"extension directory '{loc.name}' has no {ENTRY_MODULE}.py, skipped")
            elif loc.suffix == ".py":
                flats[loc.stem] = loc
        items: list[ScanItem] = []
        for name in sorted(set(packages) | set(flats)):
            if name in flats and name in packages:
                _warn(f"'{name}.py' is shadowed by package '{name}/', skipped")
            item = _distill(name, packages[name] if name in packages else flats[name])
            if item.is_err():
                issue = item.unwrap_err()
                _warn(f"extension '{issue.name}' {issue.reason}, skipped")
            items.append(item)
        return tuple(items)

default_loader = ExtensionLoader()
