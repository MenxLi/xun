"""`stat.db`: table definitions, migration and typed row access.

Each table is declared once as DDL plus its pydantic row model; INSERT/SELECT
derive from the model's fields, so SQL and Python cannot drift apart.
"""
from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.request import pathname2url

from pydantic import BaseModel, ConfigDict, Field

from xun.config import get_internal_env
from xun.extension import extension_data_dir


class Row(BaseModel):
    """One table row; validated on the way in and on the way out."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCallRow(Row):
    toolcall_id: str
    toolname: str
    user: str = ""
    timestamp: float
    success: bool | None = None


class TokenRow(Row):
    """Token usage of one model call. `prompt_tokens_cached` is provider-reported."""

    model_call_id: str
    user: str = ""
    timestamp: float
    completion_tokens: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    prompt_tokens_cached: int | None = Field(default=None, ge=0)


class BudgetRow(Row):
    """A user's `token_cost` ceiling per natural period; None means unlimited."""

    username: str
    per_day: float | None = Field(default=None, gt=0)
    per_week: float | None = Field(default=None, gt=0)
    per_month: float | None = Field(default=None, gt=0)


@dataclass(frozen=True)
class Table[R: Row]:
    name: str
    model: type[R]
    columns: tuple[str, ...]
    """One column definition each; its first word is the column name."""
    statements: tuple[str, ...] = ()
    """Extra DDL run after CREATE TABLE, e.g. indexes."""

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(self.model.model_fields)

    def definitions(self) -> dict[str, str]:
        return {column.split(maxsplit=1)[0]: column for column in self.columns}

    def create(self, conn: sqlite3.Connection) -> None:
        body = ",\n".join(f"  {column}" for column in self.columns)
        conn.execute(f"CREATE TABLE IF NOT EXISTS {self.name} (\n{body}\n)")
        for statement in self.statements:
            conn.execute(statement)

    def migrate(self, conn: sqlite3.Connection) -> None:
        """Add columns an older database lacks; nothing else is rewritten."""
        definitions = self.definitions()
        undeclared = [field for field in self.fields if field not in definitions]
        if undeclared:
            raise RuntimeError(f"table '{self.name}': field(s) without a column definition: "
                               f"{', '.join(undeclared)}")
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({self.name})")}
        for field in self.fields:
            if field not in existing:
                conn.execute(f"ALTER TABLE {self.name} ADD COLUMN {definitions[field]}")

    def insert(self, conn: sqlite3.Connection, rows: Sequence[R]) -> None:
        sql = (f"INSERT INTO {self.name} ({', '.join(self.fields)})"
               f" VALUES ({', '.join(':' + field for field in self.fields)})")
        conn.executemany(sql, [row.model_dump() for row in rows])

    def select(self, conn: sqlite3.Connection, where: str = "",
               params: dict[str, Any] | None = None, order_by: str = "") -> list[R]:
        sql = f"SELECT {', '.join(self.fields)} FROM {self.name}"
        sql += f" {where}" if where else ""
        sql += f" ORDER BY {order_by}" if order_by else ""
        return [self.model.model_validate(dict(row))
                for row in conn.execute(sql, params or {})]


TOOLCALL = Table(
    name="toolcall",
    model=ToolCallRow,
    columns=(
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "toolcall_id TEXT NOT NULL",
        "toolname TEXT NOT NULL",
        "user TEXT NOT NULL DEFAULT ''",
        "timestamp REAL NOT NULL",
        "success BOOLEAN CHECK (success IS NULL OR success IN (0, 1))",
    ),
    statements=(
        "CREATE INDEX IF NOT EXISTS idx_toolcall_toolcall_id ON toolcall (toolcall_id)",
        "CREATE INDEX IF NOT EXISTS idx_toolcall_timestamp ON toolcall (timestamp)",
    ),
)

TOKEN = Table(
    name="token",
    model=TokenRow,
    columns=(
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "model_call_id TEXT NOT NULL",
        "user TEXT NOT NULL DEFAULT ''",
        "timestamp REAL NOT NULL",
        "completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0)",
        "prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0)",
        "total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0)",
        "prompt_tokens_cached INTEGER CHECK (prompt_tokens_cached IS NULL OR prompt_tokens_cached >= 0)",
    ),
    statements=(
        "CREATE INDEX IF NOT EXISTS idx_token_timestamp ON token (timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_token_user_timestamp ON token (user, timestamp)",
    ),
)

TOKEN_BUDGET = Table(
    name="token_budget",
    model=BudgetRow,
    columns=(
        "username TEXT PRIMARY KEY",
        "per_day REAL CHECK (per_day IS NULL OR per_day > 0)",
        "per_week REAL CHECK (per_week IS NULL OR per_week > 0)",
        "per_month REAL CHECK (per_month IS NULL OR per_month > 0)",
    ),
)

TABLES = (TOOLCALL, TOKEN, TOKEN_BUDGET)

DATA_VERSION = "1"


def default_db_path() -> Path:
    return extension_data_dir("stat", DATA_VERSION) / "stat.db"


def current_username() -> str:
    return get_internal_env("USERNAME") or ""


@contextlib.contextmanager
def writer(db_path: Path) -> Iterator[sqlite3.Connection]:
    with contextlib.closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        conn.row_factory = sqlite3.Row
        yield conn


@contextlib.contextmanager
def reader(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Read-only connection: safe next to writers, and it never creates the file."""
    with contextlib.closing(
        sqlite3.connect(f"file:{pathname2url(str(db_path))}?mode=ro", uri=True, timeout=10)
    ) as conn:
        conn.row_factory = sqlite3.Row
        yield conn


def init_db(db_path: Path) -> Path:
    """Create or upgrade every table, then check schema and model agree."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with writer(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        for table in TABLES:
            table.create(conn)
            table.migrate(conn)
    return db_path
