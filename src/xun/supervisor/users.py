from __future__ import annotations

import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..config import get_home_dir


_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class User:
    name: str
    token: str
    generation: int = 0
    """Bumped by `xunx upgrade`; containers are recreated once they lag behind it."""
    paused: bool = False

    @property
    def base_path(self) -> str:
        return f"/{self.name}"


class UserStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_home_dir() / "x" / "xunx.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "name TEXT PRIMARY KEY, "
                "token TEXT NOT NULL UNIQUE, "
                "generation INTEGER NOT NULL DEFAULT 0, "
                "paused INTEGER NOT NULL DEFAULT 0"
                ")"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            if "generation" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
                )
            if "paused" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"
                )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add(self, name: str) -> User:
        if not _USERNAME_PATTERN.fullmatch(name):
            raise ValueError("username may only contain letters, numbers, '_' and '-'")
        user = User(name=name, token=secrets.token_urlsafe(24))
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO users (name, token) VALUES (?, ?)",
                    (user.name, user.token),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"user already exists: {name}") from error
        return user

    def delete(self, name: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM users WHERE name = ?", (name,))
        return cursor.rowcount > 0

    def get(self, name: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT name, token, generation, paused FROM users WHERE name = ?", (name,)
            ).fetchone()
        return User(name=row[0], token=row[1], generation=row[2], paused=bool(row[3])) if row else None

    def list(self) -> list[User]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, token, generation, paused FROM users ORDER BY name"
            ).fetchall()
        return [
            User(name=name, token=token, generation=generation, paused=bool(paused))
            for name, token, generation, paused in rows
        ]

    def set_paused(self, name: str, paused: bool) -> User | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET paused = ? WHERE name = ?", (int(paused), name)
            )
        return self.get(name)

    def upgrade(self, name: str) -> User | None:
        """Mark one user's container for recreation on the next reconciliation."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET generation = generation + 1 WHERE name = ?", (name,)
            )
        return self.get(name)