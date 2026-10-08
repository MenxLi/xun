"""CLI entry: `show` the statistics, or read/set `budget`s per user.

    python -m extensions.stat.cli show --days 7
    python -m extensions.stat.cli budget set --user alice --day 50K --month 1M
    python -m extensions.stat.cli budget show

The db is `default_db_path()` — the same `$XUN_HOME` (else `cwd/.xun`) the agent
resolves, so run this from where the agent runs, or pass `--db`.
Budget numbers are `token_cost` units (see pricing.py); 'none' clears a ceiling.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from . import budget
from .db import current_username, default_db_path, init_db
from .pricing import parse_count
from .query import WINDOWS, Filters, PeriodKind, parse_date, query_stats, time_window, window_label
from .report import budget_line, print_report

CLEAR = ("none", "null", "-")
"""Anything meaning 'no ceiling' on the command line."""


def _limits(args: argparse.Namespace) -> dict[PeriodKind, float | None]:
    """The --day/--week/--month flags, parsed as counts; absent flags stay absent."""
    limits: dict[PeriodKind, float | None] = {}
    for window in WINDOWS:
        raw: str | None = getattr(args, window.kind)
        if raw is None:
            continue
        if raw.strip().lower() in CLEAR:
            limits[window.kind] = None
            continue
        try:
            limits[window.kind] = parse_count(raw)
        except ValueError:
            raise SystemExit(f"Bad --{window.kind} value {raw!r}: try '500', '50K', '1.5M' or 'none'")
    return limits


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db) if args.db else default_db_path()


def _show(args: argparse.Namespace) -> int:
    db_path = _db_path(args)
    if not db_path.exists():
        Console().print(f"[red]stat.db not found:[/red] {db_path}")
        return 1
    since, end = time_window(args.days, args.until)
    print_report(query_stats(db_path, Filters(since=since, until=end, user=args.user)),
                 budget.usage(db_path, args.user or current_username()),
                 db_path, window_label(args.days, args.until, args.user), args.top)
    return 0


def _budget_show(args: argparse.Namespace) -> int:
    db_path = _db_path(args)
    console = Console()
    rows = budget.all_rows(db_path)
    if not rows:
        console.print(f"[dim]no budget set, every user is unlimited · {db_path}[/dim]")
        return 0
    console.print(f"[bold]token budgets[/bold] [dim]{db_path}[/dim]")
    for row in rows:
        console.print(f"[bold]{row.username or '-'}[/bold] · "
                      f"{budget_line(budget.usage(db_path, row.username))}")
    return 0


def _budget_set(args: argparse.Namespace) -> int:
    limits = _limits(args)
    if not limits:
        Console().print("[red]Nothing to set: pass --day / --week / --month.[/red]")
        return 2
    db_path = init_db(_db_path(args))
    row = budget.set_limits(db_path, args.user or current_username(), limits)
    Console().print(f"[green]budget set for {row.username or '-'}[/green] · "
                    + budget_line(budget.usage(db_path, row.username)) + f" [dim]{db_path}[/dim]")
    return 0


def _budget_clear(args: argparse.Namespace) -> int:
    username = args.user if args.user is not None else current_username()
    db_path = _db_path(args)
    removed = budget.clear(db_path, username)
    Console().print((f"[green]budget cleared for[/green] {username or '-'}" if removed
                     else f"[yellow]no budget row for[/yellow] {username or '-'}")
                    + f" [dim]{db_path}[/dim]")
    return 0 if removed else 1


def _db_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", metavar="PATH",
                        help="stat.db path (default: <xun home>/extension_data/stat/, "
                             "where <xun home> is $XUN_HOME else cwd/.xun)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stat", description="xun tool-call / token statistics and per-user token budgets.")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="show tool-call and token statistics")
    _db_option(show)
    show.add_argument("--user", metavar="NAME",
                      help="only this user's rows and budget (default: all rows, current user's budget)")
    show.add_argument("--days", type=int, metavar="N", help="only the last N days (relative to --until)")
    show.add_argument("--until", type=parse_date, metavar="YYYYMMDD",
                      help="cutoff date, inclusive (default: today)")
    show.add_argument("--top", type=int, default=15, metavar="N", help="max rows per table (default: 15)")
    show.set_defaults(handler=_show)

    budgets = sub.add_parser("budget", help="read or set per-user token_cost budgets")
    budget_sub = budgets.add_subparsers(dest="action", required=True)

    listed = budget_sub.add_parser("show", help="every budget row with what is spent so far")
    _db_option(listed)
    listed.set_defaults(handler=_budget_show)

    setter = budget_sub.add_parser("set", help="set ceilings; periods left out keep their value")
    _db_option(setter)
    setter.add_argument("--user", metavar="NAME", help="whose budget (default: the current user)")
    for window in WINDOWS:
        setter.add_argument(f"--{window.kind}", metavar="COST",
                            help=f"{window.label} ceiling in token_cost units, e.g. 50K or 'none'")
    setter.set_defaults(handler=_budget_set)

    clearer = budget_sub.add_parser("clear", help="remove a user's budget row")
    _db_option(clearer)
    clearer.add_argument("--user", metavar="NAME", help="whose budget (default: the current user)")
    clearer.set_defaults(handler=_budget_clear)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
