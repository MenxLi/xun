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
from typing import TYPE_CHECKING
from dataclasses import asdict, dataclass, field

from ..core.agent_state import JsonEntry
from ..core.config import get_home_dir
from ..core.display_abstract import DisplayAbstract
from ..core.hooks import HookArgs
from ..core.util import atomic_write
from ..core.workspace import DeferredTempDirectory, Workspace
from .storage import AgentStore, DBEventStore

if TYPE_CHECKING:
    from typing import Callable

    from ..core.agent import Agent
    from ..core.display_store import EventStore

    # A factory builds the display around the session's event store, plus agents.
    # FreshFactory creates them from scratch; SessionFactory loads them from saved slots.
    # Agent should assign the display on the factory, will initialize by the session.
    type FreshFactory[D: DisplayAbstract] = Callable[
        ["EventStore"], tuple[D, list["Agent[Agent.T.Uninit]"]]]
    type SessionFactory[D: DisplayAbstract] = Callable[
        ["EventStore", list[AgentStore]], tuple[D, list["Agent[Agent.T.Uninit]"]]]

_SAFE_ID = re.compile(r"[\w.-]+\Z", re.ASCII)  # a single, traversal-free path segment

# Marks an agent as a session member (only members are saved). `Agent.inherit`
# never copies state, so sub-agents never carry it over.
MEMBER_KEY = "xun.session.member"

def _sessions_root(home_dir: Path | None) -> Path:
    return (home_dir or get_home_dir()) / "sessions"

def _session_dir(session_id: str, home_dir: Path | None) -> Path:
    if not session_id.strip(".") or not _SAFE_ID.fullmatch(session_id):
        raise ValueError(f"Unsafe session id: {session_id!r}")
    return _sessions_root(home_dir) / session_id

def _agent_stores(session_dir: Path) -> list[AgentStore]:
    """Saved agent slots, oldest first (slot names are random hex)."""
    found: list[tuple[float, str, AgentStore]] = []
    for slot in (session_dir / "agents").glob("*"):
        store = AgentStore(slot)
        if slot.is_dir() and store.exists():
            found.append((store.created_at(), slot.name, store))
    return [store for _, _, store in sorted(found)]

def _open[DT: DisplayAbstract](
    directory: Path,
    name: str,
    created_at: float,
    event_store: DBEventStore,
    display: DT,
    agents: list["Agent[Agent.T.Uninit]"],
) -> "DurableSession[DT]":
    session = DurableSession(directory, name, created_at, display, event_store)
    for agent in agents:
        session._attach(agent)
    return session

@dataclass(frozen=True)
class SessionMeta:
    name: str
    created_at: float  # born once: written by new(), never rewritten
    saved_at: float

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, session_dir: Path, *, fallback_name: str) -> "SessionMeta":
        """Missing or damaged metadata yields a freshly born meta under the fallback name."""
        try:
            data = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
            now = time.time()  # older snapshots: missing timestamps claim now
            return cls(data["name"], data.get("created_at", now), data.get("saved_at", now))
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
    _ended: bool = False

    @staticmethod
    def new[DT: DisplayAbstract](
        session_id: str | None = None,
        *,
        name: str | None = None,
        home_dir: Path | None = None,
        factory: "FreshFactory[DT]",
    ) -> "DurableSession[DT]":
        session_id = session_id or secrets.token_hex(6)  # git-style hex
        directory = _session_dir(session_id, home_dir)
        if directory.exists():
            raise FileExistsError(f"Session already exists: {session_id!r}")
        directory.mkdir(parents=True)
        event_store = DBEventStore(directory / "events.db")
        display, agents = factory(event_store)
        return _open(directory, name or session_id, time.time(), event_store, display, agents)

    @staticmethod
    def from_saved[DT: DisplayAbstract](
        session_id: str,
        *,
        home_dir: Path | None = None,
        factory: "SessionFactory[DT]",
    ) -> "DurableSession[DT]":
        """Missing or damaged `session.json` falls back to the id."""
        directory = _session_dir(session_id, home_dir)
        if not directory.is_dir():
            raise FileNotFoundError(f"No such session: {session_id!r}")
        stored = SessionMeta.load(directory, fallback_name=session_id)
        event_store = DBEventStore(directory / "events.db")
        display, agents = factory(event_store, _agent_stores(directory))
        return _open(directory, stored.name, stored.created_at, event_store, display, agents)

    @property
    def session_id(self) -> str:
        return self.directory.name

    @property
    def mount_path(self) -> str:
        return f"/{self.session_id}"

    def _slot(self, identifier: str) -> AgentStore:
        return AgentStore(self.directory / "agents" / identifier)

    def _attach(self, agent: "Agent[Agent.T.Uninit]") -> "Agent[Agent.T.Init]":
        """Adopt a factory-built agent, then initialize it: shared workspace,
        member marker, run-end save."""
        if self._ended:
            raise RuntimeError("Session already ended")
        workspace = self.directory / "workspace"
        tempdir = self.directory / "tempdir"
        workspace.mkdir(exist_ok=True)
        tempdir.mkdir(exist_ok=True)
        agent.workspace = Workspace(workdir=workspace, tempdir=DeferredTempDirectory(tempdir))
        agent.state[MEMBER_KEY] = JsonEntry(True)

        def save(args: HookArgs.RunArgs) -> None:
            if self._ended:
                return  # a late save must not resurrect an ended session
            self._slot(args.agent.identifier).save(args.agent)
            self.save_meta()

        self.save_meta()  # discoverable from its first agent on: a killed container never reaches close()
        agent.hooks.run_end.add(save)
        return agent.initialize()

    def _members(self) -> list["Agent[Agent.T.Alive]"]:
        """The display's binds, filtered to members (drops transient co-residents)."""
        return [a for a in list(self.display.agents.values()) if MEMBER_KEY in a.state]

    def close(self) -> None:
        """End the session keeping its data. Idempotent."""
        if self._ended:
            return
        self.save_meta()  # before ending: save_meta is a no-op once ended
        self._ended = True
        for agent in self._members():  # bind order keeps closing events chronological
            agent.finalize()
            self._slot(agent.identifier).save(agent)
        self._store.close()

    def save_meta(self) -> None:
        """Write session.json now, e.g. to persist a rename without a run."""
        if self._ended:
            return
        meta = SessionMeta(name=self.name, created_at=self.created_at, saved_at=time.time())
        atomic_write(self.directory / "session.json", json.dumps(meta.to_json()).encode("utf-8"))

    def destroy(self) -> None:
        """Release members without saving and erase the directory. Idempotent."""
        self._ended = True
        for agent in self._members():
            agent.finalize()
        self._store.close()  # safe on an already-closed store
        shutil.rmtree(self.directory, ignore_errors=True)

    @staticmethod
    def discover[DT: DisplayAbstract](
        factory: "SessionFactory[DT]",
        home_dir: Path | None = None,
    ) -> list["DurableSession[DT]"]:
        """Stored sessions, oldest first. """
        directories = [
            directory
            for directory in _sessions_root(home_dir).glob("*")
            if _SAFE_ID.fullmatch(directory.name) and (directory / "session.json").is_file()
        ]
        directories.sort(key=lambda d: (SessionMeta.load(d, fallback_name=d.name).created_at, d.name))
        return [
            DurableSession.from_saved(d.name, home_dir=home_dir, factory=factory)
            for d in directories
        ]
