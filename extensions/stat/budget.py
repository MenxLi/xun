"""Per-user `token_cost` budgets: stored ceilings, spent-so-far usage, and the gate."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Mapping

from xun import CancelledError

from .db import TOKEN_BUDGET, BudgetRow, reader, writer
from .pricing import format_count
from .query import WINDOWS, BudgetUsage, PeriodKind, query_usage


def _get(conn: sqlite3.Connection, username: str) -> BudgetRow | None:
    rows = TOKEN_BUDGET.select(conn, "WHERE username = :username", {"username": username})
    return rows[0] if rows else None


def limits_of(row: BudgetRow | None) -> dict[PeriodKind, float | None]:
    return {window.kind: (getattr(row, window.column) if row else None) for window in WINDOWS}


def get(db_path: Path, username: str) -> BudgetRow | None:
    if not db_path.exists():
        return None
    with reader(db_path) as conn:
        return _get(conn, username)


def all_rows(db_path: Path) -> list[BudgetRow]:
    if not db_path.exists():
        return []
    with reader(db_path) as conn:
        return TOKEN_BUDGET.select(conn, order_by="username")


def set_limits(db_path: Path, username: str, limits: Mapping[PeriodKind, float | None]) -> BudgetRow:
    """Upsert the ceilings named in `limits`; unmentioned periods keep their value."""
    with writer(db_path) as conn:
        values = {**limits_of(_get(conn, username)), **limits}
        row = BudgetRow(username=username,
                        **{window.column: values[window.kind] for window in WINDOWS})
        conn.execute("DELETE FROM token_budget WHERE username = :username", {"username": username})
        TOKEN_BUDGET.insert(conn, [row])
        return row


def clear(db_path: Path, username: str) -> bool:
    """Delete a user's budget row; reports whether there was one."""
    if not db_path.exists():
        return False
    with writer(db_path) as conn:
        return conn.execute(
            "DELETE FROM token_budget WHERE username = :username", {"username": username}
        ).rowcount > 0


def usage(db_path: Path, username: str, now: datetime | None = None) -> BudgetUsage:
    return query_usage(db_path, username, limits_of(get(db_path, username)), now)


def check_budget(db_path: Path, username: str, now: datetime | None = None) -> None:
    """Refuse a run (before_execution) whose already-spent cost reached a ceiling."""
    exceeded = usage(db_path, username, now).exceeded
    if exceeded:
        spent = ", ".join(
            f"{period.label} {format_count(period.cost)} >= budget {format_count(period.budget)}"
            for period in exceeded
        )
        raise CancelledError(f"Token budget exceeded for user '{username or '-'}': {spent}")
