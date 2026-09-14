from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..config import get_home_dir


_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class User:
    name: str
    token: str

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
                "token TEXT NOT NULL UNIQUE"
                ")"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

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

    def list(self) -> list[User]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, token FROM users ORDER BY name"
            ).fetchall()
        return [User(name=name, token=token) for name, token in rows]