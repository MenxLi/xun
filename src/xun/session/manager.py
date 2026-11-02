"""Session layout and save timing for managed sessions (web deployments).

A manager owns `<home>/sessions/<session_id>/`: the display event database
plus one slot per agent (`<agent_id>/`) holding its snapshot and a managed
workspace (`workspace/`, `tempdir/`). Agents save on every run end;
`finalize()` and `destroy()` end the session by keeping or erasing it.
"""

from __future__ import annotations

import re
import secrets
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.config import get_home_dir
from ..core.workspace import DeferredTempDirectory, Workspace
from .storage import AgentStore, DBEventStore

if TYPE_CHECKING:
    from ..core.agent import Agent

_SAFE_ID = re.compile(r"[\w.-]+\Z", re.ASCII)


class SessionManager:
    """Owns one session directory and the save timing of its agents."""

    def __init__(self, session_id: str | None = None, *, home_dir: Path | None = None):
        session_id = session_id or secrets.token_hex(6)  # git-style hex, plenty for a local namespace
        if not session_id.strip(".") or not _SAFE_ID.fullmatch(session_id):
            raise ValueError(f"Unsafe session id: {session_id!r}")
        self.session_id = session_id
        self._dir = (home_dir or get_home_dir()) / "sessions" / session_id
        self._store: DBEventStore | None = None
        self._agents: dict[str, tuple["Agent[Agent.T.Any]", AgentStore]] = {}

    @property
    def directory(self) -> Path:
        return self._dir

    def event_store(self) -> DBEventStore:
        """The session's event database; pass it to `WebDisplay(event_store=...)`
        so the display's buffer size applies to it."""
        if self._store is None:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._store = DBEventStore(self._dir / "events.db")
        return self._store

    def attach(self, agent: "Agent[Agent.T.Uninit]") -> AgentStore:
        """Give `agent` a managed workspace and save it on every run end.
        Call before `initialize()`; when restoring, load the snapshot first so
        the identifier lands back in its own slot."""
        if agent.identifier in self._agents:
            raise ValueError(f"Agent already attached: {agent.identifier}")
        slot = self._dir / agent.identifier
        workspace = slot / "workspace"
        tempdir = slot / "tempdir"
        workspace.mkdir(parents=True, exist_ok=True)
        tempdir.mkdir(exist_ok=True)
        agent.workspace = Workspace(workdir=workspace, tempdir=DeferredTempDirectory(tempdir))
        store = AgentStore(slot)
        self._agents[agent.identifier] = (agent, store)
        agent.hooks.run_end.add(lambda args: store.save(args.agent))
        return store

    def finalize(self) -> None:
        """Save every attached agent and close the event store. Idempotent;
        finalize the agents themselves first — their closing events belong in the store."""
        for agent, store in self._agents.values():
            store.save(agent)
        if self._store is not None:
            self._store.close()
            self._store = None

    def destroy(self) -> None:
        """Discard the session: close the event store, erase the directory, skip saves."""
        if self._store is not None:
            self._store.close()
            self._store = None
        shutil.rmtree(self._dir, ignore_errors=True)

    def entries(self) -> dict[str, AgentStore]:
        """Snapshot slots present in this session, keyed by agent id."""
        entries: dict[str, AgentStore] = {}
        for slot in sorted(self._dir.glob("*")):
            store = AgentStore(slot)
            if slot.is_dir() and store.exists():
                entries[slot.name] = store
        return entries

    @classmethod
    def discover(cls, *, home_dir: Path | None = None) -> list["SessionManager"]:
        """All stored sessions under `<home>/sessions/`."""
        root = (home_dir or get_home_dir()) / "sessions"
        return [
            cls(directory.name, home_dir=home_dir)
            for directory in sorted(root.glob("*"))
            if directory.is_dir() and _SAFE_ID.fullmatch(directory.name)
        ]
