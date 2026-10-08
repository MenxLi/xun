"""Presentation of a `Stats` + `BudgetUsage` snapshot: agent HTML and CLI console."""
from __future__ import annotations

import html
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich import box

from .pricing import format_count
from .query import BudgetUsage, Stats

UNLIMITED = "unlimited"


def budget_line(usage: BudgetUsage) -> str:
    return " · ".join(
        f"{period.label} {format_count(period.cost)} / "
        + (format_count(period.budget) if period.budget is not None else UNLIMITED)
        for period in usage.periods
    )


def _tokens_line(stats: Stats) -> str:
    t = stats.tokens
    cached = f", cached {format_count(t.cached_tokens)}" if t.cached_tokens else ""
    hit = f" ({t.cache_hit_rate:.0%} hit)" if t.cache_hit_rate else ""
    return (f"completion {format_count(t.completion_tokens)}"
            f" · prompt {format_count(t.prompt_tokens)}{cached}{hit}"
            f" · total {format_count(t.total_tokens)}")


def stats_html(stats: Stats, usage: BudgetUsage, top: int) -> str:
    """HTML; <p>/<table> blocks keep it readable after tag-stripping."""
    e = html.escape

    def rate(r: float | None) -> str:
        if r is None:
            return "-"
        color = "green" if r >= 0.9 else "orange" if r >= 0.7 else "red"
        return f'<span style="color:{color}">{r:.1%}</span>'

    # the trailing space collapses in browsers but separates words once tags are stripped
    def tr(tag: str, *vals: object) -> str:
        cells = "".join(
            f'<{tag} style="text-align:{"left" if i == 0 else "right"};padding:1px 8px">{v} </{tag}>'
            for i, v in enumerate(vals)
        )
        return f"<tr>{cells}</tr>"

    t = stats.tokens
    parts = [
        f"<p>Tool calls <b>{stats.tools.calls:,}</b> ({stats.tools.ok} ok / {stats.tools.fail} fail"
        f" / {stats.tools.pending} pending), success {rate(stats.tools.success_rate)}</p>",
        f"<p>Model calls <b>{t.model_calls:,}</b>"
        + (f" · {t.events:,} usage events" if t.events != t.model_calls else "") + "</p>",
        f"<p>Tokens: <b>{_tokens_line(stats)}</b> · cost <b>{format_count(t.cost)}</b></p>",
        f"<p>Budget ({e(usage.user or '-')}): {budget_line(usage)}</p>",
    ]
    if stats.known_users:
        users = ", ".join(e(u) for u in sorted(stats.known_users) if u) or "-"
        parts.append(f'<p><span style="color:orange">No data for this user.</span> Users in this db: {users}</p>')
    parts.append("<p>Tool call frequency:</p><table>"
                 + tr("th", "Tool", "Calls", "OK", "Fail", "Pending", "Success"))
    for tool in stats.by_tool[:top]:
        parts.append(tr("td", e(tool.toolname), f"{tool.calls:,}", tool.ok, tool.fail,
                        tool.pending, rate(tool.success_rate)))
    parts.append("</table><p>Daily usage:</p><table>"
                 + tr("th", "Date", "Calls", "Success", "Completion", "Prompt", "Cached", "Total", "Cost"))
    for day in stats.daily[:top]:
        parts.append(tr("td", day.day, f"{day.calls:,}", rate(day.success_rate),
                        format_count(day.completion_tokens), format_count(day.prompt_tokens),
                        format_count(day.cached_tokens), format_count(day.total_tokens),
                        format_count(day.cost)))
    parts.append("</table>")
    return "".join(parts)


def print_report(stats: Stats, usage: BudgetUsage, db_path: Path, label: str, top: int) -> None:
    console = Console()
    t = stats.tools
    tok = stats.tokens

    def rate(r: float | None) -> str:
        if r is None:
            return "[dim]-[/dim]"
        color = "green" if r >= 0.9 else "yellow" if r >= 0.7 else "red"
        return f"[{color}]{r:.1%}[/{color}]"

    console.print(f"[bold]xun stats[/bold] [dim]{db_path} · {label}[/dim]")
    if stats.known_users:
        users = " · ".join(f"[yellow]{u}[/yellow]" if u else "[dim]-[/dim]" for u in sorted(stats.known_users))
        console.print(f"[yellow]no data for user {usage.user}[/yellow] [dim]· users in this db: {users}[/dim]")

    overview = Table(box=box.SIMPLE_HEAVY, show_header=False, title="Overview")
    overview.add_column(style="cyan")
    overview.add_row("Tool calls", f"{t.calls:,}  ([green]{t.ok} ok[/green] / [red]{t.fail} fail[/red]"
                                   f" / [dim]{t.pending} pending[/dim]), success {rate(t.success_rate)}")
    overview.add_row("Model calls", f"{tok.model_calls:,}" if tok.model_calls == tok.events
                     else f"{tok.model_calls:,} [dim]({tok.events:,} usage events)[/dim]")
    overview.add_row("Tokens", _tokens_line(stats))
    overview.add_row("Cost", f"[bold]{format_count(tok.cost)}[/bold]")
    overview.add_row("Budget", budget_line(usage))
    console.print(overview)

    tools = Table(box=box.SIMPLE, title=f"Tool call frequency (top {top})", header_style="bold cyan")
    for name in ("Tool", "Calls", "OK", "Fail", "Pending", "Success"):
        tools.add_column(name, justify="left" if name == "Tool" else "right")
    for tool in stats.by_tool[:top]:
        tools.add_row(tool.toolname, f"{tool.calls:,}", str(tool.ok), str(tool.fail),
                      str(tool.pending), rate(tool.success_rate))
    console.print(tools)

    daily = Table(box=box.SIMPLE, title=f"Daily usage (most recent {min(top, len(stats.daily))} active days)",
                  header_style="bold cyan")
    for name in ("Date", "Calls", "Success", "Completion", "Prompt", "Cached", "Total", "Cost"):
        daily.add_column(name, justify="left" if name == "Date" else "right")
    for day in stats.daily[:top]:
        daily.add_row(day.day, f"{day.calls:,}", rate(day.success_rate),
                      format_count(day.completion_tokens), format_count(day.prompt_tokens),
                      format_count(day.cached_tokens), format_count(day.total_tokens),
                      format_count(day.cost))
    console.print(daily)
