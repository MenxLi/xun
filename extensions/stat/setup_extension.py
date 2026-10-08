"""Log tool calls and token usage to SQLite and cap them with per-user budgets.

`/stat` reports; budgets are managed by `xune stat budget`.
See README.md for the schema and the cost formula.
"""
from __future__ import annotations

import time

from xun import Agent, Command, ExtensionContext, HookArgs, extension_attr
from xun.display_event import HTMLInfoEvent

from . import budget, cli
from .cli import build_parser
from .db import DATA_VERSION, TOKEN, TOOLCALL, ToolCallRow, TokenRow, current_username, init_db, writer
from .query import Filters, parse_date, query_stats, time_window, window_label
from .report import stats_html

USAGE = "Usage: /stat [days=N] [until=YYYYMMDD] [user=NAME] [top=N]"

@extension_attr(data_version=DATA_VERSION, cli=(build_parser(), cli.run))
def setup_extension(ctx: ExtensionContext) -> None:
    db_path = init_db(ctx.data_dir() / "stat.db")

    def log_tool_calls(args: HookArgs.BeforeToolCallArgs) -> None:
        registered = {tool.name for tool in ctx.agent.toolbox.list_tools(include_disabled=True)}
        user, now = current_username(), time.time()
        with writer(db_path) as conn:
            TOOLCALL.insert(conn, [
                ToolCallRow(toolcall_id=call.id, toolname=call.function.name, user=user, timestamp=now)
                for call in args.tool_calls
                if call.function.name in registered
                # providers occasionally emit calls for tools this toolbox never registered
            ])

    def log_tool_results(args: HookArgs.AfterToolCallArgs) -> None:
        with writer(db_path) as conn:
            conn.executemany(
                "UPDATE toolcall SET success = :success WHERE toolcall_id = :toolcall_id",
                [{"success": result.is_ok(), "toolcall_id": tool_call_id}
                 for tool_call_id, result in args.tool_results],
            )

    def log_token_usage(args: HookArgs.TokenMetrics) -> None:
        with writer(db_path) as conn:
            TOKEN.insert(conn, [TokenRow(
                model_call_id=args.model_call_id,
                user=current_username(),
                timestamp=time.time(),
                completion_tokens=args.completion_tokens,
                prompt_tokens=args.prompt_tokens,
                total_tokens=args.total_tokens,
                prompt_tokens_cached=args.prompt_tokens_cached,
            )])

    def check_budget(args: HookArgs.BeforeExecutionArgs) -> None:
        budget.check_budget(db_path, current_username())

    ctx.agent.hooks.before_tool_call.add(log_tool_calls)
    ctx.agent.hooks.after_tool_call.add(log_tool_results)
    ctx.agent.hooks.completion_token_update.add(log_token_usage)
    ctx.agent.hooks.before_execution.add(check_budget)

    def stat_command(agent: Agent[Agent.T.Init], args: list[str]) -> None:
        """Show tool-call and token statistics.

        k=v options (all optional):
            days=N          only the last N days
            until=YYYYMMDD  cutoff date, inclusive (default today)
            user=NAME       only this user's rows (default all); the budget line is this user's
            top=N           max rows per table (default 15)

        Examples:  /stat   /stat days=7   /stat user=alice until=20261001 top=10
        """
        opts: dict[str, str] = {}
        for arg in args:
            key, sep, value = arg.partition("=")
            if not sep or not value:
                agent.error(f"Expected k=v, got {arg!r}. {USAGE}")
                return
            opts[key.lower()] = value
        if unknown := set(opts) - {"days", "until", "user", "top"}:
            agent.error(f"Unknown option(s): {', '.join(sorted(unknown))}. {USAGE}")
            return
        try:
            days = int(opts["days"]) if "days" in opts else None
            top = int(opts["top"]) if "top" in opts else 15
            until = parse_date(opts["until"]) if "until" in opts else None
        except ValueError as ex:
            agent.error(f"Bad option value: {ex}")
            return
        since, end = time_window(days, until)
        user = opts.get("user")
        agent.display_event(HTMLInfoEvent(
            title=f"xun stats · {window_label(days, until, user)}",
            html=stats_html(query_stats(db_path, Filters(since=since, until=end, user=user)),
                            budget.usage(db_path, user or current_username()), top),
        ))

    ctx.agent.command.register(Command("stat", stat_command))
