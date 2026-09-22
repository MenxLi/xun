"""
Global extension system: drop Python files under `{XUN_HOME}/extensions/` and
every agent picks up their effects at initialization.

Two source forms, each yielding an extension named `{name}`:
- package form: `extensions/{name}/setup_extension.py` (supports relative imports)
- flat form:    `extensions/{name}.py` (zero ceremony; no relative imports)
"""
from __future__ import annotations
import functools
import importlib.util
import inspect
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Callable, cast
import rich
from .config import get_home_dir
from .types import CancelledError
if TYPE_CHECKING:
    from .agent import Agent

ENTRY_MODULE = "setup_extension"
"""Fixed entry function (and package-form file) name."""
ENTRY_FILE = f"{ENTRY_MODULE}.py"

@dataclass(frozen=True)
class Extension:
    name: str
    """Defaults to the extension's directory / file name."""
    description: str
    """Defaults to the module docstring."""
    setup: Callable[["ExtensionContext"], None]
    """The setup_extension function to initialize the extension."""
    path: Path
    """The setup_extension.py (or flat .py) it came from."""

@dataclass(frozen=True)
class ExtensionContext:
    """Passed to `setup_extension`; fresh per `initialize()`."""
    _ext: Extension
    agent: "Agent[Agent.T.Uninit]"

    @property
    def name(self) -> str:
        return self._ext.name

def _warn(msg: str) -> None:
    rich.print(f"[bold yellow]Extension warning:[/bold yellow] {msg}")

def _import_ext_module(name: str, location: Path) -> ModuleType | None:
    """Import one extension source as module `{pkg}.setup_extension` (package) or `{pkg}` (flat),
    returning the module holding the entry function. sys.modules entries inserted here are kept
    alive on success (sub-agent replay relies on them); removed on failure."""
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
    except Exception as e:
        for key in inserted:
            for k in [k for k in sys.modules if k == key or k.startswith(f"{key}.")]:
                del sys.modules[k]
        _warn(f"extension '{name}' failed to import, skipped: {e}")
        return None

def _distill(name: str, location: Path) -> Extension | None:
    mod = _import_ext_module(name, location)
    if mod is None:
        return None
    setup = getattr(mod, ENTRY_MODULE, None)
    if not callable(setup):
        _warn(f"extension '{name}' has no callable '{ENTRY_MODULE}()', skipped")
        return None
    doc = inspect.getdoc(mod) or ""
    return Extension(
        name=name,
        description=doc.splitlines()[0] if doc else "",
        setup=cast("Callable[[ExtensionContext], None]", setup),
        path=location if location.is_file() else location / ENTRY_FILE,
    )

@functools.lru_cache(maxsize=16)
def _scan_extensions(home_dir: Path) -> tuple[Extension, ...]:
    """Scan + import once per home dir / process; sorted by name (deterministic across forms)."""
    ext_root = home_dir / "extensions"
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
    loaded: list[Extension] = []
    for name in sorted(set(packages) | set(flats)):
        if name in flats and name in packages:
            _warn(f"'{name}.py' is shadowed by package '{name}/', skipped")
        ext = _distill(name, packages[name] if name in packages else flats[name])
        if ext is not None:
            loaded.append(ext)
    return tuple(loaded)

_SCAN_LOCK = threading.Lock()
def _load_extensions() -> tuple[Extension, ...]:
    with _SCAN_LOCK:
        return _scan_extensions(get_home_dir())

def list_loaded_extensions() -> list[Extension]:
    """All loaded extensions, in application order. Triggers the (cached) scan if needed."""
    return list(_load_extensions())

def apply_extensions(agent: "Agent[Agent.T.Uninit]") -> None:
    """Gate on config, then invoke each cached extension's setup with a fresh ctx.
    A failing extension warns and never blocks the rest or agent startup."""
    if not agent.config.enable_extensions:
        return
    for ext in _load_extensions():
        try:
            ext.setup(ExtensionContext(_ext=ext, agent=agent))
        except (KeyboardInterrupt, CancelledError):
            raise
        except Exception as e:
            _warn(f"extension '{ext.name}' setup failed (agent startup continues): {e}")
