from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Optional
import weakref

class DeferredTempDirectory:
    """
    An abstraction for temporary directory,
    if pth is given, no cleanup will be performed. 
    Otherwise, a temporary directory will be lazily created and automatically cleaned up when the instance is garbage collected.
    """
    def __init__(self, pth: Optional[Path] = None):
        self._dir = pth
        self._temp_dir: Optional[TemporaryDirectory] = None
        self._lock = Lock()
        if self._dir is not None:
            assert self._dir.exists() and self._dir.is_dir(), f"Path {self._dir} does not exist or is not a directory."
        
        # note: the callback must not hold a strong reference to the instance
        # (weakref.finalize keeps its arguments alive), hence the weakref idiom
        self_ref = weakref.ref(self)
        weakref.finalize(self, DeferredTempDirectory._destroy, self_ref)

    @property
    def path(self) -> Path:
        with self._lock:
            if self._dir is not None:
                return self._dir
            else:
                if self._temp_dir is None:
                    self._temp_dir = TemporaryDirectory(
                        prefix="xun-", 
                        suffix="-temp",
                    )
                return Path(self._temp_dir.name)
    
    @property
    def exist_path(self) -> Optional[Path]:
        with self._lock:
            if self._dir is not None:
                return self._dir
            else:
                return self._temp_dir and Path(self._temp_dir.name)
    
    @staticmethod
    def _destroy(this_ref: "weakref.ref"):
        # the referent is still alive when a weakref.finalize callback runs
        if (this := this_ref()) is None:
            return
        with this._lock:
            if this._temp_dir is not None:
                this._temp_dir.__exit__(None, None, None)
                this._temp_dir = None

@dataclass
class ResolvedPath:
    path: Path
    in_workdir: bool
    in_tempdir: bool

    @property
    def valid(self) -> bool:
        """Whether the path falls within an area owned by the workspace."""
        return self.in_workdir or self.in_tempdir

@dataclass
class Workspace:
    """
    The agent's on-disk footprint, unifying the file areas an agent owns:
      - `workdir`: the persistent working directory, root for relative paths and scope checks.
      - `tempdir`: a lazily created scratch area, cleaned up on GC; shared via `Agent.inherit`.
    """
    workdir: Path = field(default_factory=lambda: Path.cwd())
    tempdir: DeferredTempDirectory = field(default_factory=DeferredTempDirectory)

    def prepare(self) -> None:
        """Ensure the workdir exists as a directory. Called during agent initialization."""
        if self.workdir.exists():
            assert self.workdir.is_dir(), f"Workdir path {self.workdir} must be a directory."
        else:
            self.workdir.mkdir(parents=False, exist_ok=True)

    def resolve(self, path: str | Path, raise_on_invalid: bool = True) -> ResolvedPath:
        """Resolve `path` against the workdir and classify which owned area it falls in."""
        p = Path(path)
        base = self.workdir if not p.is_absolute() else Path()
        resolved = base / p if not p.is_absolute() else p

        # check
        cwd_abs = self.workdir.resolve()
        resolved_abs = resolved.resolve()
        if (temp_dir := self.tempdir.exist_path) is not None:
            temp_dir_abs = temp_dir.resolve()
            in_tempdir = resolved_abs == temp_dir_abs or temp_dir_abs in resolved_abs.parents
        else:
            in_tempdir = False
        in_workdir = resolved_abs.is_relative_to(cwd_abs)
        if raise_on_invalid and not (in_workdir or in_tempdir):
            raise ValueError(f"Path {resolved_abs} is not within the agent's workspace (workdir or temporary directory).")
        return ResolvedPath(resolved, in_workdir, in_tempdir)
