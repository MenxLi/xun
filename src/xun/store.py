from pathlib import Path

class Store:
    def __init__(self, root_dir: Path = Path(".xun")):
        self._root_dir = root_dir
        self._init_structure()
    
    @property
    def root_dir(self) -> Path:
        return self._root_dir
    
    @property
    def conversation_dir(self) -> Path:
        return self._root_dir / "conversation"
    
    def _init_structure(self):
        self.root_dir.mkdir(exist_ok=True)
        self.conversation_dir.mkdir(exist_ok=True)
    
    def latest_history_store(self) -> Path | None:
        """ Find the latest conversation history directory.  """
        history_dirs = list(self.conversation_dir.glob("*.conversation"))
        if not history_dirs:
            return None
        history_dirs.sort(reverse=True)
        return history_dirs[0]
    
    def get_history_store(self, idx: int | str) -> Path | None:
        if isinstance(idx, int):
            idx_str = f"{idx:06d}"
        else:
            idx_str = str(idx)
        if not idx_str.endswith(".conversation"):
            idx_str += ".conversation"
        history_dir = self.conversation_dir / idx_str
        if history_dir.exists() and history_dir.is_dir():
            return history_dir
        return None
    
    def next_history_store(self) -> Path:
        """ Find the next conversation history directory, without creating it.  """
        latest = self.latest_history_store()
        if not latest:
            return self.conversation_dir / f"{0:06d}.conversation"
        latest_num = int(latest.stem)
        next_num = latest_num + 1
        return self.conversation_dir / f"{next_num:06d}.conversation"
    