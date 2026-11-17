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

The display and event store live off memory until first use: `display` materializes
them (rebuilding every saved agent), and `offload()` releases them again. Close,
destroy, and discover never wake a resting session; `fork` wakes only its copy.

Implements the `xun.displays.WebSession` protocol.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING
from dataclasses import asdict, dataclass, field

from ..core.agent_state import JsonEntry
from ..core.config import get_home_dir
from ..core.display_abstract import DisplayAbstract
from ..core.display_event import AgentBindEvent, AgentUnbindEvent
from ..core.hooks import HookArgs
from ..core.util import atomic_write
from ..core.workspace import DeferredTempDirectory, Workspace
from .storage import AgentStore, DBEventStore

if TYPE_CHECKING:
    from typing import Callable

    from ..core.agent import Agent
    from ..core.display_store import EventStore

    # A factory builds the display around the session's event store, plus agents.
    # Empty stores means fresh birth; otherwise the factory `store.load`s each slot
    # into its agent. Agents must assign the display on the factory; the session
    # initializes them. Called on every materialization, so it must be repeatable.
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

@dataclass
class _Live[D: DisplayAbstract]:
    """A session's resident generation: store and display are born and die together."""

    store: DBEventStore
    display: D

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
    """The session root. Construct through `new()`, `from_saved()`, or `fork()`.

    Thread-safe (`_lock` is reentrant: `_attach` initializes agents under it):
    the session is resident only while someone holds `display` or is serving it."""

    directory: Path
    name: str
    created_at: float
    _factory: "SessionFactory[D]" = field(repr=False)
    _live: "_Live[D] | None" = field(default=None, repr=False)
    _ended: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @staticmethod
    def new[DT: DisplayAbstract](
        session_id: str | None = None,
        *,
        name: str | None = None,
        home_dir: Path | None = None,
        factory: "SessionFactory[DT]",
    ) -> "DurableSession[DT]":
        session_id = session_id or secrets.token_hex(6)  # git-style hex
        directory = _session_dir(session_id, home_dir)
        if directory.exists():
            raise FileExistsError(f"Session already exists: {session_id!r}")
        directory.mkdir(parents=True)
        return DurableSession(directory, name or session_id, time.time(), factory)

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
        return DurableSession(directory, stored.name, stored.created_at, factory)

    @property
    def display_alive(self) -> "D | None":
        """The display while resident; None when resting. Never materializes."""
        return self._live.display if self._live is not None else None

    @property
    def display(self) -> D:
        """The session's display, materialized on first use (see `offload`)."""
        with self._lock:
            return self._materialize().display

    @property
    def _store(self) -> DBEventStore:
        with self._lock:
            return self._materialize().store

    def _materialize(self) -> "_Live[D]":
        """Build store+display and adopt the saved agents (caller holds `_lock`)."""
        if self._ended:
            raise RuntimeError("Session already ended")
        if self._live is None:
            event_store = DBEventStore(self.directory / "events.db")
            display, agents = self._factory(event_store, _agent_stores(self.directory))
            if display.event_store is not event_store:
                raise RuntimeError("Display's event store does not match the session's event store")
            self._live = _Live(event_store, display)
            with display.suppress_filter(lambda e: isinstance(e.payload, AgentBindEvent)):
                for agent in agents:
                    self._adopt(agent)
        return self._live

    def offload(self) -> None:
        """Release the resident generation: members are saved and finalized, then the
        display and store are dropped. The session stays open and re-materializes on
        the next `display`. Idempotent; a no-op on an ended or resting session."""
        with self._lock:
            if not self._ended:
                self._release(save=True)

    def fork(self, name: str | None = None) -> "DurableSession[D]":
        """A complete copy of this session (files, agents, history) born fresh as a
        sibling: new id and created_at, `name` defaults to "<this> (fork)"; agents
        keep their identifiers, so the copied timeline stays continuous. The source
        is not woken; the copy is, once, to hand its agents off the source's paths."""
        if not self.directory.is_dir():
            raise FileNotFoundError(f"No such session: {self.session_id!r}")
        session_id = secrets.token_hex(6)
        directory = self.directory.parent / session_id
        # session.json would carry the source's birth; events.db is copied online below.
        shutil.copytree(self.directory, directory, ignore=shutil.ignore_patterns("session.json", "events.db*"))
        forked: "DurableSession[D] | None" = None
        try:
            if (source_db := self.directory / "events.db").exists():
                with closing(sqlite3.connect(source_db)) as src, \
                        closing(sqlite3.connect(directory / "events.db")) as dst:
                    src.backup(dst)
            forked = DurableSession(directory, name or f"{self.name} (fork)", time.time(), self._factory)
            forked.save_meta()
            workdir = forked.directory / "workspace"
            for agent in forked.display.agents.values():
                agent.conversation.add_developer_message(
                    f"This session was forked from another session; its workspace was copied "
                    f"to {workdir}, which is now the working directory. Paths under "
                    f"{self.directory} appear in earlier messages but belong to the source "
                    "session, which may still live: never read or write them. Use paths "
                    "relative to the working directory from now on. "
                    "Also use your new temporary directories for any transient files."
                )
            return forked
        except BaseException:
            # destroy, when available: closes the store a failed wake may have opened
            forked.destroy() if forked else shutil.rmtree(directory, ignore_errors=True)
            raise

    @property
    def session_id(self) -> str:
        return self.directory.name

    @property
    def mount_path(self) -> str:
        return f"/{self.session_id}"

    def _slot(self, identifier: str) -> AgentStore:
        return AgentStore(self.directory / "agents" / identifier)

    def _attach(self, agent: "Agent[Agent.T.Uninit]") -> "Agent[Agent.T.Init]":
        """Materialize if resting, then adopt and initialize a factory-built agent."""
        with self._lock:
            self._materialize()
            return self._adopt(agent)

    def _adopt(self, agent: "Agent[Agent.T.Uninit]") -> "Agent[Agent.T.Init]":
        """Adopt a factory-built agent, then initialize it: shared workspace,
        member marker, run-end save."""
        workspace = self.directory / "workspace"
        tempdir = self.directory / "tempdir"
        workspace.mkdir(exist_ok=True)
        tempdir.mkdir(exist_ok=True)
        agent.workspace = Workspace(workdir=workspace, tempdir=DeferredTempDirectory(tempdir))
        agent.state[MEMBER_KEY] = JsonEntry(True)

        def save(args: HookArgs.RunArgs) -> None:
            if self._ended or args.agent.display is not self.display_alive:
                return  # a late save must not touch an ended or offloaded generation
            self._slot(args.agent.identifier).save(args.agent)
            self.save_meta()

        self.save_meta()  # discoverable from its first agent on: a killed container never reaches close()
        agent.hooks.run_end.add(save)
        return agent.initialize()

    def _members(self) -> list["Agent[Agent.T.Alive]"]:
        """The resident display's binds, filtered to members (drops transient
        co-residents). Never materializes: a resting session has no members."""
        live = self._live
        if live is None:
            return []
        return [a for a in list(live.display.agents.values()) if MEMBER_KEY in a.state]

    def close(self) -> None:
        """End the session keeping its data. Idempotent. Never wakes a resting session."""
        self.save_meta()  # before ending: save_meta is a no-op once ended
        with self._lock:
            if self._ended:
                return
            self._release(save=True)
            self._ended = True

    def _release(self, *, save: bool) -> None:
        """Drop the resident generation (caller holds `_lock`); resting is a no-op."""
        live = self._live
        if live is None:
            return
        with live.display.suppress_filter(lambda e: isinstance(e.payload, AgentUnbindEvent)):
            for agent in self._members():  # bind order keeps closing events chronological
                agent.finalize()
                if save:
                    self._slot(agent.identifier).save(agent)
        live.store.close()
        self._live = None

    def rename(self, name: str) -> None:
        self.name = name
        self.save_meta()

    def save_meta(self) -> None:
        """Write session.json now, e.g. to persist a rename without a run."""
        if self._ended:
            return
        meta = SessionMeta(name=self.name, created_at=self.created_at, saved_at=time.time())
        atomic_write(self.directory / "session.json", json.dumps(meta.to_json()).encode("utf-8"))

    def destroy(self) -> None:
        """Release members without saving and erase the directory. Idempotent.
        Never wakes a resting session."""
        with self._lock:
            self._ended = True
            self._release(save=False)
            shutil.rmtree(self.directory, ignore_errors=True)  # resting: the rmtree is all

    @staticmethod
    def discover[DT: DisplayAbstract](
        factory: "SessionFactory[DT]",
        home_dir: Path | None = None,
    ) -> list["DurableSession[DT]"]:
        """Stored sessions, oldest first."""
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
