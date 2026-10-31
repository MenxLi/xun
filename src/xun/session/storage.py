"""Session persistence: agent snapshots and durable display events.

Mechanism only: these classes raise, callers present. A snapshot is data,
never an `Agent` construction: `save` dumps name, conversation, persist
state, and workspace; `load` pours them back into an uninitialized agent.

`DBEventStore` is the persistent event backend for displays: an independent
replay channel, related to snapshots only through the agent identifier.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.config import get_home_dir, xun_version
from ..core.display_event import DisplayEvent
from ..core.workspace import Workspace

if TYPE_CHECKING:
    from ..core.agent import Agent


@dataclass(frozen=True)
class SnapshotMeta:
    name: str
    identifier: str
    saved_at: str
    xun_version: str | None
    workspace: dict
    """`Workspace.to_json` payload, kept raw: restoring is the workspace's business."""

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "identifier": self.identifier,
            "saved_at": self.saved_at,
            "xun_version": self.xun_version,
            "workspace": self.workspace,
        }

    @classmethod
    def from_json(cls, data: dict) -> "SnapshotMeta":
        return cls(
            name=data["name"],
            identifier=data["identifier"],
            saved_at=data["saved_at"],
            xun_version=data.get("xun_version"),
            workspace=data["workspace"],
        )


class AgentStore:
    """Read and write a single session snapshot directory."""

    def __init__(self, directory: Path | None = None):
        self._dir = directory or get_home_dir() / "session"

    def exists(self) -> bool:
        return (self._dir / "meta.json").is_file()

    def save(self, agent: "Agent[Agent.T.Any]") -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._write("meta.json", SnapshotMeta(
            name=agent.name,
            identifier=agent.identifier,
            saved_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            xun_version=xun_version(),
            workspace=agent.workspace.to_json(),
        ).to_json())
        agent.conversation.dump(self._dir / "conversation.json")
        self._write("state.json", agent.state.to_json())

    def load(self, agent: "Agent[Agent.T.Uninit]") -> list[str]:
        """Pour the snapshot into an uninitialized agent: name, identifier,
        conversation, persist state (runtime entries kept), workspace (degraded
        when paths are gone). Returns degradation notes.

        Call before `initialize()` so the first display bind uses the restored identifier.
        """
        meta = SnapshotMeta.from_json(self._read("meta.json"))
        agent.name = meta.name
        agent.identifier = meta.identifier
        agent.conversation.load(self._dir / "conversation.json")
        agent.state.load_persist(self._read("state.json"))
        return self._restore_workspace(agent, meta.workspace)

    def _read(self, name: str) -> dict:
        return json.loads((self._dir / name).read_text(encoding="utf-8"))

    def _write(self, name: str, data: dict) -> None:
        (self._dir / name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _restore_workspace(agent: "Agent[Agent.T.Uninit]", data: dict) -> list[str]:
        """Graft the saved footprint onto the live workspace; missing paths degrade to the live ones."""
        notes: list[str] = []
        restored = Workspace.from_json(data)
        if restored.workdir.is_dir():
            agent.workspace.workdir = restored.workdir
        else:
            notes.append(f"workdir {restored.workdir} no longer exists, keeping {agent.workspace.workdir}")
        if data.get("tempdir"):
            if restored.tempdir.external_path is not None:
                agent.workspace.tempdir = restored.tempdir
            else:
                notes.append(f"tempdir {data['tempdir']} no longer exists, using a fresh one")
        return notes


class DBEventStore:
    """Persistent event store backed by one SQLite file, structurally satisfying
    `core.display_store.EventStore`.

    Install with `display.event_store = ...`, then `display.with_buffer_size(n)`
    if a retention cap is wanted. Writes commit per event by default;
    `manual_commit=True` batches them until `flush()`. `close()` always flushes.
    """

    def __init__(self, path: Path | None = None, *, manual_commit: bool = False):
        path = path or get_home_dir() / "session" / "events.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                data TEXT NOT NULL
            )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS events_agent ON events(agent_id)")
        self._conn.commit()
        self._lock = threading.Lock()
        self._manual = manual_commit
        self._max_events: int | None = None

    def set_buffer_size(self, max_events: int | None) -> None:
        with self._lock:
            self._max_events = max_events
            self._prune()
            self._maybe_commit()

    def count(self, agent_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM events" + (" WHERE agent_id = ?" if agent_id is not None else "")
        with self._lock:
            return self._conn.execute(sql, () if agent_id is None else (agent_id,)).fetchone()[0]

    def select(
        self,
        agent_id: str | None = None,
        offset: int = 0,
        limit: int | None = None,
        from_latest: bool = False,
    ) -> list[DisplayEvent]:
        sql = "SELECT data FROM events"
        params: list = []
        if agent_id is not None:
            sql += " WHERE agent_id = ?"
            params.append(agent_id)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?" if from_latest else " ORDER BY id LIMIT ? OFFSET ?"
        params += [-1 if limit is None else limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [DisplayEvent.from_json(json.loads(row[0])) for row in rows]

    def clear(self, agent_id: str | None = None) -> None:
        sql = "DELETE FROM events" + (" WHERE agent_id = ?" if agent_id is not None else "")
        with self._lock:
            self._conn.execute(sql, () if agent_id is None else (agent_id,))
            self._maybe_commit()

    def add(self, event: DisplayEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(agent_id, timestamp, data) VALUES (?, ?, ?)",
                (event.agent.identifier, event.timestamp, json.dumps(event.model_dump(mode="json"), ensure_ascii=False)),
            )
            self._prune()
            self._maybe_commit()

    def flush(self) -> None:
        """Persist writes accumulated since the last commit (no-op otherwise).
        The only durability point besides `close()` when `manual_commit` is set."""
        with self._lock:
            self._conn.commit()

    def agent_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT agent_id FROM events GROUP BY agent_id ORDER BY MIN(id)").fetchall()
        return [row[0] for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    def _maybe_commit(self) -> None:
        if not self._manual:
            self._conn.commit()

    def _prune(self) -> None:
        """Drop rows beyond the retention cap (no commit; caller holds the lock)."""
        if self._max_events is None:
            return
        self._conn.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (self._max_events,))
