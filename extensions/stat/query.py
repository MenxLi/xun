"""Read-side aggregation over `stat.db`: filters, natural-period windows, stats and usage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict

from .db import reader
from .pricing import PRICING

PeriodKind = Literal["day", "week", "month"]


@dataclass(frozen=True)
class Window:
    """A natural calendar period, and the `token_budget` column that caps it."""

    kind: PeriodKind
    column: str
    label: str

    def start(self, now: datetime) -> datetime:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.kind == "week":
            return midnight - timedelta(days=now.weekday())  # ISO week, Monday first
        if self.kind == "month":
            return midnight.replace(day=1)
        return midnight


WINDOWS: tuple[Window, ...] = (
    Window("day", "per_day", "today"),
    Window("week", "per_week", "this week"),
    Window("month", "per_month", "this month"),
)


class Model(BaseModel):
    """A read model: fields match the SQL column aliases exactly."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Filters(BaseModel):
    """Row filter shared by every stats query."""

    since: float | None = None
    """inclusive lower bound"""
    until: float | None = None
    """exclusive upper bound"""
    user: str | None = None

    def sql(self) -> tuple[str, dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        if self.since is not None:
            conds.append("timestamp >= :since")
            params["since"] = self.since
        if self.until is not None:
            conds.append("timestamp < :until")
            params["until"] = self.until
        if self.user:
            conds.append("user = :user")
            params["user"] = self.user
        return (f"WHERE {' AND '.join(conds)}" if conds else ""), params


def parse_date(value: str) -> datetime:
    """`YYYYMMDD`; dashes optional."""
    return datetime.strptime(value.replace("-", ""), "%Y%m%d")


def time_window(days: int | None, until: datetime | None) -> tuple[float | None, float | None]:
    """`(since, until)` epoch bounds; `until` is the exclusive end of that day."""
    end = (until + timedelta(days=1)).timestamp() if until else None
    since = ((end if end is not None else datetime.now().timestamp()) - days * 86400) if days else None
    return since, end


def window_label(days: int | None, until: datetime | None, user: str | None) -> str:
    label = f"last {days} days" if days else "all time"
    if until:
        label += f" until {until:%Y-%m-%d}"
    if user:
        label += f" · user {user}"
    return label


class ToolTotals(Model):
    calls: int
    ok: int
    fail: int
    pending: int

    @property
    def success_rate(self) -> float | None:
        judged = self.ok + self.fail
        return self.ok / judged if judged else None


class ToolFrequency(ToolTotals):
    toolname: str


class TokenTotals(Model):
    events: int
    model_calls: int
    completion_tokens: int
    prompt_tokens: int
    cached_tokens: int
    total_tokens: int
    cost: float
    """`token_cost` summed over the filtered rows, priced by `PRICING`."""

    @property
    def cache_hit_rate(self) -> float | None:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else None


class DayTotals(Model):
    day: str
    calls: int = 0
    ok: int = 0
    fail: int = 0
    completion_tokens: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0

    @property
    def success_rate(self) -> float | None:
        judged = self.ok + self.fail
        return self.ok / judged if judged else None


class Stats(Model):
    tools: ToolTotals
    tokens: TokenTotals
    by_tool: list[ToolFrequency]
    daily: list[DayTotals]
    known_users: list[str] = []
    """Filled in only when a user filter matched nothing, to catch a typo."""


_OK_FAIL_PENDING = """COALESCE(SUM(success = 1), 0) AS ok,
       COALESCE(SUM(success = 0), 0) AS fail,
       COALESCE(SUM(success IS NULL), 0) AS pending"""

_DAY = "date(timestamp, 'unixepoch', 'localtime')"


def query_stats(db_path: Path, filters: Filters) -> Stats:
    """One read-only pass over stat.db, shared by the /stat command and the CLI."""
    where, params = filters.sql()
    cost = PRICING.cost_sql("token")
    with reader(db_path) as conn:
        tools = ToolTotals.model_validate(dict(conn.execute(
            f"SELECT COUNT(*) AS calls, {_OK_FAIL_PENDING} FROM toolcall {where}", params).fetchone()))
        tokens = TokenTotals.model_validate(dict(conn.execute(f"""
            SELECT COUNT(*) AS events,
                   COUNT(DISTINCT model_call_id) AS model_calls,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(COALESCE(prompt_tokens_cached, 0)), 0) AS cached_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM({cost}), 0.0) AS cost
            FROM token {where}""", params).fetchone()))
        by_tool = [ToolFrequency.model_validate(dict(r)) for r in conn.execute(
            f"SELECT toolname, COUNT(*) AS calls, {_OK_FAIL_PENDING}"
            f" FROM toolcall {where} GROUP BY toolname ORDER BY calls DESC, toolname", params)]
        daily_tools = {row["day"]: dict(row) for row in conn.execute(
            f"SELECT {_DAY} AS day, COUNT(*) AS calls,"
            f" COALESCE(SUM(success = 1), 0) AS ok, COALESCE(SUM(success = 0), 0) AS fail"
            f" FROM toolcall {where} GROUP BY day", params)}
        daily_tokens = {row["day"]: dict(row) for row in conn.execute(f"""
            SELECT {_DAY} AS day,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(COALESCE(prompt_tokens_cached, 0)), 0) AS cached_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM({cost}), 0.0) AS cost
            FROM token {where} GROUP BY day""", params)}
        known_users: list[str] = []
        if filters.user and not tools.calls and not tokens.events:
            # a filter matching nothing is most likely a typo; only then pay for this scan
            known_users = [row["user"] for row in conn.execute(
                "SELECT DISTINCT user FROM toolcall UNION SELECT DISTINCT user FROM token")]

    daily = [
        DayTotals.model_validate({**daily_tools.get(day, {}), **daily_tokens.get(day, {}), "day": day})
        for day in sorted(set(daily_tools) | set(daily_tokens), reverse=True)
    ]
    return Stats(tools=tools, tokens=tokens, by_tool=by_tool, daily=daily, known_users=known_users)


class PeriodCost(Model):
    """Cost spent in one period, against its budget."""

    kind: PeriodKind
    label: str
    cost: float
    budget: float | None = None
    """None = unlimited."""

    @property
    def exhausted(self) -> bool:
        return self.budget is not None and self.cost >= self.budget


class BudgetUsage(Model):
    user: str
    periods: list[PeriodCost]

    @property
    def exceeded(self) -> list[PeriodCost]:
        return [period for period in self.periods if period.exhausted]


def query_usage(db_path: Path, user: str, limits: Mapping[PeriodKind, float | None],
                now: datetime | None = None) -> BudgetUsage:
    """`token_cost` spent so far in each period, paired with its budget."""
    current = now or datetime.now()
    starts: dict[str, float] = {window.kind: window.start(current).timestamp() for window in WINDOWS}
    if db_path.exists():
        cost = PRICING.cost_sql("token")
        selects = ", ".join(
            f"COALESCE(SUM(CASE WHEN timestamp >= :{window.kind} THEN {cost} END), 0.0) AS {window.kind}"
            for window in WINDOWS
        )
        params: dict[str, Any] = {**starts, "user": user, "since": min(starts.values())}
        with reader(db_path) as conn:
            spent = dict(conn.execute(
                f"SELECT {selects} FROM token WHERE user = :user AND timestamp >= :since", params).fetchone())
    else:
        spent = {window.kind: 0.0 for window in WINDOWS}
    return BudgetUsage(
        user=user,
        periods=[
            PeriodCost(kind=window.kind, label=window.label, cost=spent[window.kind],
                       budget=limits.get(window.kind))
            for window in WINDOWS
        ],
    )
