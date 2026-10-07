"""
Statistics extension for tracking tool and token usage.
"""
from xun import ExtensionContext, HookArgs
from xun.config import get_home_dir, get_internal_env
import contextlib
import time
import sqlite3

def maybe_init_db():
    home_dir = get_home_dir()
    if not home_dir.exists():
        raise RuntimeError(f"Home directory {home_dir} does not exist, cannot initialize stat database")
    db_path = home_dir / "stat.db"
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

def setup_extension(ctx: ExtensionContext):
    db_path = maybe_init_db()

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

if __name__ == "__main__":
    import argparse
    from datetime import datetime, timedelta
    from pathlib import Path
    from urllib.request import pathname2url
    from rich import box
    from rich.console import Console
    from rich.table import Table

    def yyyymmdd(s: str) -> datetime:
        try:
            return datetime.strptime(s, "%Y%m%d")
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected YYYYMMDD, got {s!r}")

    parser = argparse.ArgumentParser(
        prog="stat.py",
        description="Query xun tool-call frequency and token usage from stat.db (read-only).",
        epilog='examples: python stat.py | python stat.py --days 7 --top 10 | python stat.py --days 7 --until 20261001',
    )
    parser.add_argument("--db", metavar="PATH", help="stat.db path (default: <xun home>/stat.db)")
    parser.add_argument("--user", metavar="NAME", help="only include this user (default: all users)")
    parser.add_argument("--days", type=int, metavar="N", help="only include the last N days (relative to --until)")
    parser.add_argument("--until", type=yyyymmdd, metavar="YYYYMMDD", help="cutoff date, inclusive (default: today)")
    parser.add_argument("--top", type=int, default=15, metavar="N", help="max rows per table (default: 15)")
    args = parser.parse_args()

    console = Console()
    db_path = Path(args.db) if args.db else get_home_dir() / "stat.db"
    if not db_path.exists():
        console.print(f"[red]stat.db not found:[/red] {db_path}")
        raise SystemExit(1)

    # end of the --until day, local time, exclusive (the day itself is included).
    end = (args.until + timedelta(days=1)).timestamp() if args.until else None
    since = ((end if end is not None else time.time()) - args.days * 86400) if args.days else None
    conds: list[str] = []
    wp_list: list = []
    if since is not None:
        conds.append("timestamp > ?")
        wp_list.append(since)
    if end is not None:
        conds.append("timestamp < ?")
        wp_list.append(end)
    if args.user:
        conds.append("user = ?")
        wp_list.append(args.user)
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    wp: tuple = tuple(wp_list)

    def rate_str(ok: int, fail: int) -> str:
        judged = ok + fail
        if not judged:
            return "[dim]-[/dim]"
        r = ok / judged
        color = "green" if r >= 0.9 else "yellow" if r >= 0.7 else "red"
        return f"[{color}]{r:.1%}[/{color}]"

    def num(v: int) -> str:
        return f"{v:,}" if v else "[dim]-[/dim]"

    def per_call(total: int, calls: int) -> str:
        return f"{round(total / calls):,}" if calls else "[dim]-[/dim]"

    ok_fail_pending = "COALESCE(SUM(success = 1), 0), COALESCE(SUM(success = 0), 0), COALESCE(SUM(success IS NULL), 0)"

    with contextlib.closing(sqlite3.connect(f"file:{pathname2url(str(db_path))}?mode=ro", uri=True)) as conn:
        tool_total, ok, fail, pending = conn.execute(f"""
            SELECT COUNT(*), {ok_fail_pending}
            FROM toolcall {where}
        """, wp).fetchone()
        n_rows, n_calls, c_tok, p_tok, t_tok = conn.execute(f"""
            SELECT COUNT(*), COUNT(DISTINCT model_call_id),
                   COALESCE(SUM(completion_tokens), 0),
                   COALESCE(SUM(prompt_tokens), 0),
                   COALESCE(SUM(total_tokens), 0)
            FROM token {where}
        """, wp).fetchone()
        by_tool = conn.execute(f"""
            SELECT toolname, COUNT(*), {ok_fail_pending}
            FROM toolcall {where}
            GROUP BY toolname ORDER BY 2 DESC, toolname LIMIT ?
        """, (*wp, args.top)).fetchall()
        daily_tools = {
            d: (n, s_ok, s_fail)
            for d, n, s_ok, s_fail in conn.execute(f"""
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
        if args.user and not tool_total and not n_rows:
            # a filter matching nothing is most likely a typo; only then pay for this scan
            known_users = [row[0] for row in conn.execute(
                "SELECT DISTINCT user FROM toolcall UNION SELECT DISTINCT user FROM token"
            )]

    if args.days:
        window = f"last {args.days} days"
    else:
        window = "all time"
    if args.until:
        window += f" until {args.until:%Y-%m-%d}"
    if args.user:
        window += f" · user {args.user}"
    console.print(f"[bold]xun stats[/bold] [dim]{db_path} · {window}[/dim]")
    if known_users:
        users = " · ".join(f"[yellow]{u}[/yellow]" if u else "[dim](empty)[/dim]" for u in sorted(known_users))
        console.print(f"[yellow]no data for user {args.user}[/yellow] [dim]· users in this db: {users}[/dim]")

    overview = Table(box=box.SIMPLE_HEAVY, show_header=False, title="Overview")
    overview.add_column(style="cyan")
    overview.add_row("Tool calls", f"{tool_total:,}  ([green]{ok} ok[/green] / [red]{fail} fail[/red] / [dim]{pending} pending[/dim]), success {rate_str(ok, fail)}")
    overview.add_row("Model calls", f"{n_calls:,}" if n_calls == n_rows else f"{n_calls:,} [dim]({n_rows:,} usage events)[/dim]")
    overview.add_row("Tokens", f"completion [green]{num(c_tok)}[/green] · prompt [yellow]{num(p_tok)}[/yellow] · total [bold]{num(t_tok)}[/bold]")
    overview.add_row("Per model call", f"completion [green]{per_call(c_tok, n_calls)}[/green] · prompt [yellow]{per_call(p_tok, n_calls)}[/yellow] · total [bold]{per_call(t_tok, n_calls)}[/bold]")
    console.print(overview)

    tools = Table(box=box.SIMPLE, title=f"Tool call frequency (top {args.top})", header_style="bold cyan")
    tools.add_column("Tool")
    tools.add_column("Calls", justify="right")
    tools.add_column("OK", justify="right")
    tools.add_column("Fail", justify="right")
    tools.add_column("Pending", justify="right")
    tools.add_column("Success", justify="right")
    for name, n, s_ok, s_fail, s_pending in by_tool:
        tools.add_row(name, f"{n:,}", str(s_ok), str(s_fail), str(s_pending), rate_str(s_ok, s_fail))
    console.print(tools)

    active_days = sorted(set(daily_tools) | set(daily_tokens), reverse=True)[: args.top]
    # completion is a sum (real output); prompt/total averages per call, since their sums
    # just track how much conversation history accumulated that day.
    daily = Table(box=box.SIMPLE, title=f"Daily usage (most recent {len(active_days)} active days)", header_style="bold cyan")
    daily.add_column("Date")
    daily.add_column("Calls", justify="right")
    daily.add_column("Success", justify="right")
    daily.add_column("Completion", justify="right")
    daily.add_column("Avg prompt", justify="right")
    daily.add_column("Avg total", justify="right")
    for d in active_days:
        n, s_ok, s_fail = daily_tools.get(d, (0, 0, 0))
        c, p, t, nc = daily_tokens.get(d, (0, 0, 0, 0))
        daily.add_row(d, num(n), rate_str(s_ok, s_fail), num(c), per_call(p, nc), per_call(t, nc))
    console.print(daily)