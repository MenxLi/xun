"""A durable session: everything lives under `<home>/sessions/<session_id>/`.

    <session_id>/
        session.json    # session metadata (name, timestamps)
        events.db       # session event store (owned by the session)
        workspace/      # the session's workdir, shared by all its agents
        tempdir/        # shared scratch area
        agents/<agent_id>/
            meta.json
            conversation.json
            state.json

Implements the `xun.displays.WebSession` protocol.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from dataclasses import asdict, dataclass, field

from ..core.config import get_home_dir
from ..core.display_abstract import DisplayAbstract
from ..core.hooks import HookArgs
from ..core.workspace import DeferredTempDirectory, Workspace
from .storage import AgentStore, DBEventStore

if TYPE_CHECKING:
    from ..core.agent import Agent

_SAFE_ID = re.compile(r"[\w.-]+\Z", re.ASCII)  # a single, traversal-free path segment

def _sessions_root(home_dir: Path | None) -> Path:
    return (home_dir or get_home_dir()) / "sessions"

def _session_dir(session_id: str, home_dir: Path | None) -> Path:
    if not session_id.strip(".") or not _SAFE_ID.fullmatch(session_id):
        raise ValueError(f"Unsafe session id: {session_id!r}")
    return _sessions_root(home_dir) / session_id

def _open[DT: DisplayAbstract](
    directory: Path,
    name: str,
    created_at: float,
    display_factory: Callable[[DBEventStore], DT],
) -> "DurableSession[DT]":
    store = DBEventStore(directory / "events.db")
    return DurableSession(directory, name, created_at, display_factory(store), store)

@dataclass(frozen=True)
class SessionMeta:
    name: str
    created_at: float  # born once: written by new(), never rewritten
    saved_at: float

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "SessionMeta":
        now = time.time()  # older snapshots: missing timestamps claim now
        return cls(name=data["name"], created_at=data.get("created_at", now), saved_at=data.get("saved_at", now))

    @classmethod
    def load(cls, session_dir: Path, *, fallback_name: str) -> "SessionMeta":
        """Missing or damaged metadata yields a freshly born meta under the fallback name."""
        try:
            return cls.from_json(json.loads((session_dir / "session.json").read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            now = time.time()
            return cls(name=fallback_name, created_at=now, saved_at=now)

@dataclass(eq=False)
class DurableSession[D: DisplayAbstract]:
    """The session root. Construct through `new()` or `from_saved()`, not the generated init."""

    directory: Path
    name: str
    created_at: float
    display: D = field(repr=False)
    _store: DBEventStore = field(repr=False)
    _agents: dict[str, tuple["Agent[Agent.T.Any]", AgentStore]] = field(default_factory=dict, repr=False)
    _ended: bool = False

    @staticmethod
    def new[DT: DisplayAbstract](
        session_id: str | None = None,
        *,
        name: str | None = None,
        home_dir: Path | None = None,
        display_factory: Callable[[DBEventStore], DT],
    ) -> "DurableSession[DT]":
        session_id = session_id or secrets.token_hex(6)  # git-style hex
        directory = _session_dir(session_id, home_dir)
        if directory.exists():
            raise FileExistsError(f"Session already exists: {session_id!r}")
        directory.mkdir(parents=True)
        return _open(directory, name or session_id, time.time(), display_factory)

    @staticmethod
    def from_saved[DT: DisplayAbstract](
        session_id: str,
        *,
        home_dir: Path | None = None,
        display_factory: Callable[[DBEventStore], DT],
    ) -> "DurableSession[DT]":
        """Restore a stored session. A missing `session.json` only means it never
        had an agent; damaged metadata falls back to the id."""
        directory = _session_dir(session_id, home_dir)
        if not directory.is_dir():
            raise FileNotFoundError(f"No such session: {session_id!r}")
        stored = SessionMeta.load(directory, fallback_name=session_id)
        return _open(directory, stored.name, stored.created_at, display_factory)

    @property
    def session_id(self) -> str:
        return self.directory.name

    @property
    def mount_path(self) -> str:
        return f"/{self.session_id}"

    def attach(self, agent: "Agent[Agent.T.Uninit]") -> AgentStore:
        """Give `agent` the session's shared workspace and save it on every run
        end. Call before `initialize()`; when restoring, load first so the
        identifier lands back in its own slot."""
        if self._ended:
            raise RuntimeError("Session already ended")
        if agent.identifier in self._agents:
            raise ValueError(f"Agent already attached: {agent.identifier}")
        workspace = self.directory / "workspace"
        tempdir = self.directory / "tempdir"
        workspace.mkdir(exist_ok=True)
        tempdir.mkdir(exist_ok=True)
        agent.workspace = Workspace(workdir=workspace, tempdir=DeferredTempDirectory(tempdir))
        store = AgentStore(self.directory / "agents" / agent.identifier)
        self._agents[agent.identifier] = (agent, store)

        def save(args: HookArgs.RunArgs) -> None:
            if self._ended:
                return  # a late save must not resurrect an ended session
            store.save(args.agent)
            self._save_meta()

        self._save_meta()  # discoverable from its first agent on: a killed container never reaches close()
        agent.hooks.run_end.add(save)
        return store

    def close(self) -> None:
        """End the session keeping its data. Idempotent; a closed session can
        still be destroyed."""
        if self._ended:
            return
        self._ended = True
        for agent, store in list(self._agents.values()):  # attach order keeps closing events chronological
            agent.finalize()
            store.save(agent)
        self._agents.clear()
        self._save_meta()
        self._store.close()

    def _save_meta(self) -> None:
        meta = SessionMeta(name=self.name, created_at=self.created_at, saved_at=time.time())
        (self.directory / "session.json").write_text(json.dumps(meta.to_json()), encoding="utf-8")

    def destroy(self) -> None:
        """Release agents without saving and erase the directory. Idempotent,
        valid after `close()`."""
        self._ended = True
        for agent, _ in list(self._agents.values()):
            agent.finalize()
        self._agents.clear()
        self._store.close()  # safe on an already-closed store
        shutil.rmtree(self.directory, ignore_errors=True)

    def entries(self) -> dict[str, AgentStore]:
        """Snapshot slots, oldest agent first (slot ids are random hex)."""
        found: list[tuple[float, str, AgentStore]] = []
        for slot in (self.directory / "agents").glob("*"):
            store = AgentStore(slot)
            if slot.is_dir() and store.exists():
                found.append((store.created_at(), slot.name, store))
        return {agent_id: store for _, agent_id, store in sorted(found)}

    @staticmethod
    def discover[DT: DisplayAbstract](
        display_factory: Callable[[DBEventStore], DT],
        home_dir: Path | None = None,
    ) -> list["DurableSession[DT]"]:
        """Stored sessions, oldest first. A directory without `session.json`
        never had an agent and is skipped."""
        sessions = [
            DurableSession.from_saved(directory.name, home_dir=home_dir, display_factory=display_factory)
            for directory in _sessions_root(home_dir).glob("*")
            if _SAFE_ID.fullmatch(directory.name) and (directory / "session.json").is_file()
        ]
        return sorted(sessions, key=lambda session: (session.created_at, session.session_id))
