from typing import Optional
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
import weakref
from .context import global_context_guard

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
        
        with global_context_guard as global_context:
            global_context.tempdirs.add(self)

        def maybe_cleanup_temp_dir():
            if self._temp_dir is not None:
                self._temp_dir.__exit__(None, None, None)
                self._temp_dir = None
            with global_context_guard as global_context:
                global_context.tempdirs.discard(self)
        weakref.finalize(self, maybe_cleanup_temp_dir)

    @property
    def path(self) -> Path:
        with self._lock:
            if self._dir is not None:
                return self._dir
            else:
                if self._temp_dir is None:
                    self._temp_dir = TemporaryDirectory()
                return Path(self._temp_dir.name)
    
    @property
    def exist_path(self) -> Optional[Path]:
        with self._lock:
            if self._dir is not None:
                return self._dir
            else:
                return self._temp_dir and Path(self._temp_dir.name)
