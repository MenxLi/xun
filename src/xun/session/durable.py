"""A durable session: everything lives under `<home>/sessions/<session_id>/`.

    <session_id>/
        session.json    # session metadata (name)
        events.db       # session event store (owned by the session)
        <agent_id>/     # one slot per attached agent
            meta.json   # AgentSnapshot: state, conversation, workspace
            workspace/  # the agent's workdir
            tempdir/    # the agent's tempdir (recreated empty each load)

`DurableSession` *is* the session: it owns the display (built over the event
store at construction), the name, and the mount path, and satisfies the
`xun.displays.WebSession` protocol.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..core.config import get_home_dir
from ..core.display_abstract import DisplayAbstract
from ..core.workspace import DeferredTempDirectory, Workspace
from .storage import AgentStore, DBEventStore

if TYPE_CHECKING:
    from ..core.agent import Agent

_SAFE_ID = re.compile(r"[\w.-]+\Z", re.ASCII)  # a single, traversal-free path segment

class DurableSession[D: DisplayAbstract]:
    """Durable session root managing agents, their workspaces, 
    and the display name and event store."""

    def __init__(
        self,
        session_id: str | None = None,
        *,
        name: str | None = None,
        home_dir: Path | None = None,
        display_factory: Callable[[DBEventStore], D],
    ):
        session_id = session_id or secrets.token_hex(6)  # git-style hex, plenty for a local namespace
        if not session_id.strip(".") or not _SAFE_ID.fullmatch(session_id):
            raise ValueError(f"Unsafe session id: {session_id!r}")
        self.session_id = session_id
        self.name = name or session_id
        self._dir = (home_dir or get_home_dir()) / "sessions" / session_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._store = DBEventStore(self._dir / "events.db")
        self.display = display_factory(self._store)
        self._agents: dict[str, tuple["Agent[Agent.T.Any]", AgentStore]] = {}
        self._ended = False

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def mount_path(self) -> str:
        """Where this session is mounted; derived from the id, never stored."""
        return f"/{self.session_id}"

    def attach(self, agent: "Agent[Agent.T.Uninit]") -> AgentStore:
        """Give `agent` a managed workspace and save it on every run end.
        Call before `initialize()`; when restoring, load the snapshot first so
        the identifier lands back in its own slot. Raises once the session ended."""
        if self._ended:
            raise RuntimeError("Session already ended")
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
        # once the session has ended, a late save must not resurrect it
        agent.hooks.run_end.add(lambda args: None if self._ended else store.save(args.agent))
        return store

    def close(self) -> None:
        """End the session keeping its data: finalize agents (their closing
        events land in the store), save each agent, remember the name, close
        the store. Idempotent; a closed session can still be destroyed."""
        if self._ended:
            return
        self._ended = True
        while self._agents:
            agent, store = self._agents.popitem()[1]
            agent.finalize()
            store.save(agent)
        (self._dir / "session.json").write_text(json.dumps({"name": self.name}), encoding="utf-8")
        self._store.close()

    def destroy(self) -> None:
        """Discard the session: release agents without saving and erase the
        directory. Idempotent, and valid after `close()`; nothing survives it."""
        self._ended = True
        while self._agents:
            self._agents.popitem()[1][0].finalize()
        self._store.close()  # safe on an already-closed store
        shutil.rmtree(self._dir, ignore_errors=True)

    def entries(self) -> dict[str, AgentStore]:
        """Snapshot slots present in this session, keyed by agent id."""
        entries: dict[str, AgentStore] = {}
        for slot in sorted(self._dir.glob("*")):
            store = AgentStore(slot)
            if slot.is_dir() and store.exists():
                entries[slot.name] = store
        return entries

    @staticmethod
    def discover[DT: DisplayAbstract](
        display_factory: Callable[[DBEventStore], DT],
        home_dir: Path | None = None,
    ) -> list["DurableSession[DT]"]:
        """Stored sessions under `<home>/sessions/`, with names restored from
        `session.json`. A directory without it was skipped."""
        root = (home_dir or get_home_dir()) / "sessions"
        sessions: list[DurableSession[DT]] = []
        for directory in sorted(root.glob("*")):
            meta = directory / "session.json"
            if not directory.is_dir() or not _SAFE_ID.fullmatch(directory.name) or not meta.is_file():
                continue
            try:
                name: str | None = json.loads(meta.read_text(encoding="utf-8"))["name"]
            except (OSError, ValueError, KeyError):
                name = None  # damaged metadata: fall back to the directory name
            sessions.append(
                DurableSession(directory.name, name=name, home_dir=home_dir, display_factory=display_factory)
            )
        return sessions
