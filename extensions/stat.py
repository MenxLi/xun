"""
Statistics extension for tracking tool and token usage.
"""
from xun import Command, ExtensionContext, HookArgs
from xun.config import get_home_dir, get_internal_env
from xun.display_event import HTMLInfoEvent
import contextlib
import html
import time
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import pathname2url

def default_db_path() -> Path:
    return get_home_dir() / "extension_data" / "stat" / "stat.db"

def maybe_init_db(db_path: Path):
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS toolcall (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            toolcall_id TEXT, 
            toolname TEXT, 
            user TEXT, 
            timestamp FLOAT,
            success BOOLEAN default null
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS token (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_call_id TEXT,
            user TEXT,
            timestamp FLOAT,
            completion_tokens INTEGER,
            prompt_tokens INTEGER,
            total_tokens INTEGER
        )
        """)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_toolcall_toolcall_id ON toolcall (toolcall_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_toolcall_timestamp ON toolcall (timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_token_timestamp ON token (timestamp)")
    return db_path

def get_username():
    return get_internal_env("USERNAME") or ""

@contextlib.contextmanager
def open_db(db_path):
    with contextlib.closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        yield conn

def parse_yyyymmdd(value: str) -> datetime:
    return datetime.strptime(value.replace("-", ""), "%Y%m%d")

def time_window(days: int | None, until: datetime | None) -> tuple[float | None, float | None]:
    """`(since, end)` epoch bounds; end is the exclusive end of the until day."""
    end = (until + timedelta(days=1)).timestamp() if until else None
    since = ((end if end is not None else time.time()) - days * 86400) if days else None
    return since, end

def window_label(days: int | None, until: datetime | None, user: str | None) -> str:
    label = f"last {days} days" if days else "all time"
    if until:
        label += f" until {until:%Y-%m-%d}"
    if user:
        label += f" · user {user}"
    return label

_OK_FAIL_PENDING = "COALESCE(SUM(success = 1), 0), COALESCE(SUM(success = 0), 0), COALESCE(SUM(success IS NULL), 0)"

def rate_of(ok: int, fail: int) -> float | None:
    judged = ok + fail
    return ok / judged if judged else None

def avg_of(total: int, calls: int) -> int | None:
    return round(total / calls) if calls else None

@dataclass(frozen=True)
class ToolRow:
    name: str
    calls: int
    ok: int
    fail: int
    pending: int

    @property
    def rate(self) -> float | None:
        return rate_of(self.ok, self.fail)

@dataclass(frozen=True)
class DayRow:
    date: str
    calls: int
    ok: int
    fail: int
    completion: int
    prompt: int
    total: int
    model_calls: int

    @property
    def rate(self) -> float | None:
        return rate_of(self.ok, self.fail)

    @property
    def avg_prompt(self) -> int | None:
        return avg_of(self.prompt, self.model_calls)

    @property
    def avg_total(self) -> int | None:
        return avg_of(self.total, self.model_calls)

@dataclass(frozen=True)
class Stats:
    tool_total: int
    tool_ok: int
    tool_fail: int
    tool_pending: int
    usage_rows: int
    model_calls: int
    completion_tokens: int
    prompt_tokens: int
    total_tokens: int
    by_tool: list[ToolRow]
    daily: list[DayRow]
    known_users: list[str]

    @property
    def tool_rate(self) -> float | None:
        return rate_of(self.tool_ok, self.tool_fail)

    @property
    def avg_completion(self) -> int | None:
        return avg_of(self.completion_tokens, self.model_calls)

    @property
    def avg_prompt(self) -> int | None:
        return avg_of(self.prompt_tokens, self.model_calls)

    @property
    def avg_total(self) -> int | None:
        return avg_of(self.total_tokens, self.model_calls)

def query_stats(db_path: Path, since: float | None = None, end: float | None = None,
                user: str | None = None) -> Stats:
    """One read-only pass over stat.db, shared by the /stat command and the CLI."""
    conds: list[str] = []
    params: list = []
    if since is not None:
        conds.append("timestamp > ?")
        params.append(since)
    if end is not None:
        conds.append("timestamp < ?")
        params.append(end)
    if user:
        conds.append("user = ?")
        params.append(user)
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    wp = tuple(params)

    with contextlib.closing(sqlite3.connect(f"file:{pathname2url(str(db_path))}?mode=ro", uri=True)) as conn:
        tool_total, ok, fail, pending = conn.execute(f"""
            SELECT COUNT(*), {_OK_FAIL_PENDING}
            FROM toolcall {where}
        """, wp).fetchone()
        usage_rows, model_calls, c_tok, p_tok, t_tok = conn.execute(f"""
            SELECT COUNT(*), COUNT(DISTINCT model_call_id),
                   COALESCE(SUM(completion_tokens), 0),
                   COALESCE(SUM(prompt_tokens), 0),
                   COALESCE(SUM(total_tokens), 0)
            FROM token {where}
        """, wp).fetchone()
        by_tool = [ToolRow(*r) for r in conn.execute(f"""
            SELECT toolname, COUNT(*), {_OK_FAIL_PENDING}
            FROM toolcall {where}
            GROUP BY toolname ORDER BY 2 DESC, toolname
        """, wp)]
        daily_tools = {
            d: (n, ok_, fail_)
            for d, n, ok_, fail_ in conn.execute(f"""
                SELECT date(timestamp, 'unixepoch', 'localtime'), COUNT(*),
                       COALESCE(SUM(success = 1), 0), COALESCE(SUM(success = 0), 0)
                FROM toolcall {where} GROUP BY 1
            """, wp)
        }
        daily_tokens = {
            d: (c, p, t, nc)
            for d, c, p, t, nc in conn.execute(f"""
                SELECT date(timestamp, 'unixepoch', 'localtime'),
                       COALESCE(SUM(completion_tokens), 0),
                       COALESCE(SUM(prompt_tokens), 0),
                       COALESCE(SUM(total_tokens), 0),
                       COUNT(DISTINCT model_call_id)
                FROM token {where} GROUP BY 1
            """, wp)
        }
        known_users: list[str] = []
        if user and not tool_total and not usage_rows:
            # a filter matching nothing is most likely a typo; only then pay for this scan
            known_users = [r[0] for r in conn.execute(
                "SELECT DISTINCT user FROM toolcall UNION SELECT DISTINCT user FROM token"
            )]

    daily = [
        DayRow(d, *daily_tools.get(d, (0, 0, 0)), *daily_tokens.get(d, (0, 0, 0, 0)))
        for d in sorted(set(daily_tools) | set(daily_tokens), reverse=True)
    ]
    return Stats(tool_total, ok, fail, pending, usage_rows, model_calls,
                 c_tok, p_tok, t_tok, by_tool, daily, known_users)

def stats_html(s: Stats, top: int) -> str:
    """Render stats as HTML; <p> blocks keep it readable after tag-stripping."""
    e = html.escape
    def rate(r: float | None) -> str:
        if r is None:
            return "-"
        color = "green" if r >= 0.9 else "orange" if r >= 0.7 else "red"
        return f'<span style="color:{color}">{r:.1%}</span>'
    def num(v: int | None) -> str:
        return f"{v:,}" if v else "-"
    # first column left, the rest right; the trailing space collapses in browsers
    # but keeps stripped text (CLI) readable
    def tr(tag: str, *vals: object) -> str:
        cells = "".join(
            f'<{tag} style="text-align:{"left" if i == 0 else "right"};padding:1px 8px">{v} </{tag}>'
            for i, v in enumerate(vals)
        )
        return f"<tr>{cells}</tr>"

    parts = [
        f"<p>Tool calls <b>{s.tool_total:,}</b> ({s.tool_ok} ok / {s.tool_fail} fail / {s.tool_pending} pending), success {rate(s.tool_rate)}</p>",
        f"<p>Model calls <b>{s.model_calls:,}</b>" + (f" · {s.usage_rows:,} usage events" if s.usage_rows != s.model_calls else "") + "</p>",
        f"<p>Tokens: completion <b>{num(s.completion_tokens)}</b> · prompt {num(s.prompt_tokens)} · total {num(s.total_tokens)}</p>",
        f"<p>Per model call: completion {num(s.avg_completion)} · prompt {num(s.avg_prompt)} · total {num(s.avg_total)}</p>",
    ]
    if s.known_users:
        users = ", ".join(e(u) for u in sorted(s.known_users) if u) or "(empty)"
        parts.append(f'<p><span style="color:orange">No data for this user.</span> Users in this db: {users}</p>')
    parts.append("<p>Tool call frequency:</p><table>"
                 + tr("th", "Tool", "Calls", "OK", "Fail", "Pending", "Success"))
    for t in s.by_tool[:top]:
        parts.append(tr("td", e(t.name), f"{t.calls:,}", t.ok, t.fail, t.pending, rate(t.rate)))
    parts.append("</table><p>Daily usage:</p><table>"
                 + tr("th", "Date", "Calls", "Success", "Completion", "Avg prompt", "Avg total"))
    for d in s.daily[:top]:
        parts.append(tr("td", d.date, num(d.calls), rate(d.rate), num(d.completion), num(d.avg_prompt), num(d.avg_total)))
    parts.append("</table>")
    return "".join(parts)

def setup_extension(ctx: ExtensionContext):
    db_path = maybe_init_db(ctx.data_dir() / "stat.db")

    def log_tool_call(args: HookArgs.BeforeToolCallArgs):
        user = get_username()
        now = time.time()
        tool_universe = set([tool.name for tool in ctx.agent.toolbox.list_tools(include_disabled=True)])
        with open_db(db_path) as conn:
            conn.executemany("""
            INSERT INTO toolcall (toolcall_id, toolname, user, timestamp)
            VALUES (?, ?, ?, ?)
            """, [
                (toolcall.id, toolcall.function.name, user, now)
                for toolcall in args.tool_calls 
                if toolcall.function.name in tool_universe
                # sometimes tool calls may include tools not registered in the toolbox, ignore them
            ])

    def log_tool_call_result(args: HookArgs.AfterToolCallArgs):
        with open_db(db_path) as conn:
            conn.executemany("""
            UPDATE toolcall
            SET success = ?
            WHERE toolcall_id = ?
            """, [
                (tool_result[1].is_ok(), tool_result[0])
                for tool_result in args.tool_results
            ])
    
    def log_token_usage(args: HookArgs.TokenMetrics):
        with open_db(db_path) as conn:
            conn.execute("""
            INSERT INTO token (model_call_id, user, timestamp, completion_tokens, prompt_tokens, total_tokens)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (
                args.model_call_id,
                get_username(),
                time.time(),
                args.completion_tokens,
                args.prompt_tokens,
                args.total_tokens,
            ))
    
    ctx.agent.hooks.before_tool_call.add(log_tool_call)
    ctx.agent.hooks.after_tool_call.add(log_tool_call_result)
    ctx.agent.hooks.completion_token_update.add(log_token_usage)

    def stat_command(agent, args: list[str]) -> None:
        """Show tool-call and token statistics.

        k=v options (all optional):
            days=N          only the last N days
            until=YYYYMMDD  cutoff date, inclusive (default today)
            user=NAME       only this user (default all)
            top=N           max rows per table (default 15)

        Examples:  /stat   /stat days=7   /stat user=alice until=20261001 top=10
        """
        opts: dict[str, str] = {}
        for arg in args:
            k, sep, v = arg.partition("=")
            if not sep or not v:
                agent.error(f"Expected k=v, got {arg!r}. Usage: /stat [days=N] [until=YYYYMMDD] [user=NAME] [top=N]")
                return
            opts[k.lower()] = v
        if unknown := set(opts) - {"days", "until", "user", "top"}:
            agent.error(f"Unknown option(s): {', '.join(sorted(unknown))}. Usage: /stat [days=N] [until=YYYYMMDD] [user=NAME] [top=N]")
            return
        try:
            days = int(opts["days"]) if "days" in opts else None
            top = int(opts["top"]) if "top" in opts else 15
            until = parse_yyyymmdd(opts["until"]) if "until" in opts else None
        except ValueError as ex:
            agent.error(f"Bad option value: {ex}")
            return
        if not db_path.exists():
            agent.error(f"stat.db not found: {db_path}")
            return
        since, end = time_window(days, until)
        agent.display_event(HTMLInfoEvent(
            title=f"xun stats · {window_label(days, until, opts.get('user'))}",
            html=stats_html(query_stats(db_path, since, end, opts.get("user")), top),
        ))

    ctx.agent.command.register(Command("stat", stat_command))

if __name__ == "__main__":
    import argparse
    from rich import box
    from rich.console import Console
    from rich.table import Table

    parser = argparse.ArgumentParser(
        prog="stat.py",
        description="Query xun tool-call frequency and token usage from stat.db (read-only).",
        epilog='examples: python stat.py | python stat.py --days 7 --top 10 | python stat.py --days 7 --until 20261001',
    )
    parser.add_argument("--db", metavar="PATH", help="stat.db path (default: <xun home>/stat.db)")
    parser.add_argument("--user", metavar="NAME", help="only include this user (default: all users)")
    parser.add_argument("--days", type=int, metavar="N", help="only include the last N days (relative to --until)")
    parser.add_argument("--until", type=parse_yyyymmdd, metavar="YYYYMMDD", help="cutoff date, inclusive (default: today)")
    parser.add_argument("--top", type=int, default=15, metavar="N", help="max rows per table (default: 15)")
    args = parser.parse_args()

    console = Console()
    db_path = Path(args.db) if args.db else default_db_path()
    if not db_path.exists():
        console.print(f"[red]stat.db not found:[/red] {db_path}")
        raise SystemExit(1)

    since, end = time_window(args.days, args.until)
    s = query_stats(db_path, since, end, args.user)

    def rate(r: float | None) -> str:
        if r is None:
            return "[dim]-[/dim]"
        color = "green" if r >= 0.9 else "yellow" if r >= 0.7 else "red"
        return f"[{color}]{r:.1%}[/{color}]"

    def num(v: int | None) -> str:
        return f"{v:,}" if v else "[dim]-[/dim]"

    console.print(f"[bold]xun stats[/bold] [dim]{db_path} · {window_label(args.days, args.until, args.user)}[/dim]")
    if s.known_users:
        users = " · ".join(f"[yellow]{u}[/yellow]" if u else "[dim](empty)[/dim]" for u in sorted(s.known_users))
        console.print(f"[yellow]no data for user {args.user}[/yellow] [dim]· users in this db: {users}[/dim]")

    overview = Table(box=box.SIMPLE_HEAVY, show_header=False, title="Overview")
    overview.add_column(style="cyan")
    overview.add_row("Tool calls", f"{s.tool_total:,}  ([green]{s.tool_ok} ok[/green] / [red]{s.tool_fail} fail[/red] / [dim]{s.tool_pending} pending[/dim]), success {rate(s.tool_rate)}")
    overview.add_row("Model calls", f"{s.model_calls:,}" if s.model_calls == s.usage_rows else f"{s.model_calls:,} [dim]({s.usage_rows:,} usage events)[/dim]")
    overview.add_row("Tokens", f"completion [green]{num(s.completion_tokens)}[/green] · prompt [yellow]{num(s.prompt_tokens)}[/yellow] · total [bold]{num(s.total_tokens)}[/bold]")
    overview.add_row("Per model call", f"completion [green]{num(s.avg_completion)}[/green] · prompt [yellow]{num(s.avg_prompt)}[/yellow] · total [bold]{num(s.avg_total)}[/bold]")
    console.print(overview)

    tools = Table(box=box.SIMPLE, title=f"Tool call frequency (top {args.top})", header_style="bold cyan")
    tools.add_column("Tool")
    tools.add_column("Calls", justify="right")
    tools.add_column("OK", justify="right")
    tools.add_column("Fail", justify="right")
    tools.add_column("Pending", justify="right")
    tools.add_column("Success", justify="right")
    for t in s.by_tool[: args.top]:
        tools.add_row(t.name, f"{t.calls:,}", str(t.ok), str(t.fail), str(t.pending), rate(t.rate))
    console.print(tools)

    # completion is a sum (real output); prompt/total are per-call averages, since their
    # sums just track how much conversation history accumulated that day.
    active = s.daily[: args.top]
    daily = Table(box=box.SIMPLE, title=f"Daily usage (most recent {len(active)} active days)", header_style="bold cyan")
    daily.add_column("Date")
    daily.add_column("Calls", justify="right")
    daily.add_column("Success", justify="right")
    daily.add_column("Completion", justify="right")
    daily.add_column("Avg prompt", justify="right")
    daily.add_column("Avg total", justify="right")
    for d in active:
        daily.add_row(d.date, num(d.calls), rate(d.rate), num(d.completion), num(d.avg_prompt), num(d.avg_total))
    console.print(daily)