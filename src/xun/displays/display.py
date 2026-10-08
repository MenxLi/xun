import hashlib, datetime
import threading
from typing import TYPE_CHECKING
import rich
import rich.box
import rich.table
import rich.console
import rich.panel
import rich.rule
import rich.markdown
import rich.markup
import rich.text

from ..display_abstract import *
from .cli_session import CliSession, CommandProvider
if TYPE_CHECKING:
    from ..agent import Agent
    from ..agent_lifecycle import T

class Display(DisplayAbstract):
    def __init__(self, event_buffer_size: int = 1000):
        self.console = rich.console.Console()
        self.lock = threading.Lock()
        self.session = CliSession()
        self.set_event_buffer_size(event_buffer_size)

    def _print(self, *args, **kwargs):
        with self.lock:
            if isinstance(args[0] if args else None, str):
                self.console.print(rich.text.Text(
                    f"[{datetime.datetime.now().strftime('%H:%M:%S')}]",
                    style="dim",
                ), end=" ")
            self.console.print(*args, **kwargs)

    def input(self, prompt: str = "", agent: "Agent[T.Init] | None" = None) -> str:
        """Read input with an agent-labelled default prompt and agent-rooted completion."""
        if not prompt:
            name = rich.markup.escape(agent.name if agent is not None else "You")
            prompt = _rl_prompt(self.console, f"[dim cyan]{name}[/dim cyan] [dim]>[/dim] ")
        return self.session.input(prompt, agent=agent)

    def set_command_provider(self, provider: CommandProvider) -> None:
        self.session.set_command_provider(provider)

    def get_choice(self, request: DisplayAbstract.ChoiceRequest) -> str:
        """Keep the request context in the input UI so background output can redraw it."""
        choices = request.choices
        extra_choice_idx = len(choices) + 1 if request.allow_extra else None
        default_idx = choices.index(request.default) + 1 if request.default in choices else None
        prompt = "\n".join(part for part in (
            f"[bold yellow]{request.title}[/bold yellow]" if request.title else None,
            f"[dim]{request.subtitle}[/dim]" if request.subtitle else None,
            request.message,
            f"[bold cyan]{request.prompt}[/bold cyan]" if request.prompt else None,
        ) if part)
        with self.session.takeover_input():
            prompt_choices = choices + (["Other (enter your own choice)"] if request.allow_extra else [])
            choice_idx = self.session.choose(_rl_prompt(self.console, prompt), prompt_choices, default_idx or 1)
            if request.allow_extra and choice_idx == extra_choice_idx:
                return self.session.transient_input(_rl_prompt(self.console, prompt + "\n[bold cyan]Enter your choice [/bold cyan]"))
            return choices[choice_idx - 1]
        

    def on_event(self, event: DisplayEvent):
        match event.payload:
            case ShowHelpEvent(): self._show_help(event)
            case ShowToolsEvent(): self._show_tools(event)
            case ShowExtensionsEvent(): self._show_extensions(event)
            case ShowHistoryEvent(): self._show_history(event)
            case ToolCallEvent(): self._show_tool_call(event)
            case ModelWorkingEvent(): ...
            case ModelMessageEvent(): self._show_model_message(event)
            case ToolResultEvent(): self._show_tool_result(event)
            case InfoEvent(): self._show_info(event)
            case HTMLInfoEvent(): self._show_html_info(event)
            case ConfirmEvent(): self._show_confirm(event)
            case WarningEvent(): self._show_warning(event)
            case ErrorEvent(): self._show_error(event)
            case UserMessageEvent(): ... # Shown by input
            case UserCommandEvent(): ... # Shown by input
            case AgentBindEvent(): self._agent_bind(event)
            case AgentUnbindEvent(): self._agent_unbind(event)
            case AgentRunningStartEvent(): self._show_agent_running_start(event)
            case AgentRunningEndEvent(): ...
            case _: self._unhandled(event)

    def _show_help(self, event: DisplayEvent[ShowHelpEvent]) -> None:
        help_event = event.payload
        table = rich.table.Table.grid(expand=True)
        table.add_column("Command", style="bold cyan", no_wrap=True)
        table.add_column("Description", style="dim")
        for cmd in help_event.commands:
            table.add_row(f"[bold white]{cmd.name}[/bold white]", cmd.description)
        self._print(table)

    def _show_tools(self, event: DisplayEvent[ShowToolsEvent]) -> None:
        tools = event.payload.tools
        if not tools:
            self._print(rich.panel.Panel("[dim]No tools registered.[/dim]", title="Tools", border_style="green", box=rich.box.ROUNDED))
            return
        table = rich.table.Table(title="Tools", box=rich.box.SIMPLE_HEAD, header_style="bold green", expand=True)
        table.add_column("Tool", style="bold cyan", no_wrap=True)
        table.add_column("Description", ratio=1)
        table.add_column("Requires", style="dim", no_wrap=True)
        for tool in tools:
            table.add_row(
                tool.name,
                tool.description or "[dim]No description provided.[/dim]",
                ", ".join(tool.required_capabilities) or "—",
            )
        self._print(table)

    def _show_extensions(self, event: DisplayEvent[ShowExtensionsEvent]) -> None:
        exts = event.payload.extensions
        title = event.payload.title
        if not exts:
            self._print(rich.panel.Panel(f"[dim]No {title.lower()} found.[/dim]", title=title, border_style="green", box=rich.box.ROUNDED))
            return
        table = rich.table.Table(title=title, box=rich.box.SIMPLE_HEAD, header_style="bold green", expand=True)
        table.add_column("Name", style="bold cyan", no_wrap=True)
        table.add_column("Description", ratio=1)
        table.add_column("Status", no_wrap=True)
        for ext in exts:
            color = {"failed": "red", "loaded": "green", "skipped": "yellow"}.get(ext.status, "dim")
            description = ext.description or "[dim]No description provided.[/dim]"
            if ext.source:
                description = f"{description}\n[dim]{ext.source}[/dim]"
            if ext.reason:
                description = f"{description}\n[{color}]{rich.markup.escape(ext.reason)}[/{color}]"
            table.add_row(ext.name, description, f"[{color}]{ext.status}[/{color}]")
        self._print(table)

    def _show_history(self, event: DisplayEvent[ShowHistoryEvent]) -> None:
        history = event.payload.history
        if not history:
            self._print(rich.panel.Panel("[dim]No history yet.[/dim]", title="Conversation History", border_style="green", box=rich.box.ROUNDED, padding=(0, 1)))
            return
        sub_panels = []
        for i, record in enumerate(history):
            if not record['content']:
                continue
            color = self._role_color(record["role"])
            row = rich.table.Table.grid(expand=True)
            row.add_column(style=f"bold {color}", width=10)
            row.add_column(ratio=1)
            row.add_row(record["role"], rich.markdown.Markdown(record["content"], code_theme="monokai", hyperlinks=True))
            sub_panels.append(rich.panel.Panel(row, border_style=color, box=rich.box.ROUNDED, padding=(0, 0)))
        self._print(rich.panel.Panel(rich.console.Group(*sub_panels), title="Conversation History", subtitle=f"[dim]{len(sub_panels)} msgs[/dim]", box=rich.box.ROUNDED, padding=(0, 1)))

    def _show_tool_call(self, event: DisplayEvent[ToolCallEvent]) -> None:
        ev = event.payload
        tool_id = hashlib.sha1(ev.tool_call_id.encode()).hexdigest()[:6]
        self._print(f"[dim]{rich.markup.escape(event.agent.name)} / tool {tool_id}[/dim] [cyan]{rich.markup.escape(ev.tool_name)}[/cyan]({self._arg_str(ev.args)})")

    def _show_agent_running_start(self, event: DisplayEvent[AgentRunningStartEvent]) -> None:
        msg = f"[dim]{rich.markup.escape(event.agent.name)} / running[/dim]"
        self._print(msg)

    def _show_model_message(self, event: DisplayEvent[ModelMessageEvent]) -> None:
        ev = event.payload
        def fmt_tokens(t: int):
            if t > 1000:
                return f"{t/1000:.1f}k"
            return f"{t}"
        if not ev.content.strip(): 
            return
        self._print(rich.console.Group(
            rich.text.Text.assemble(
                ("──── ", "dim cyan"),
                (event.agent.name, "bold cyan"),
            ),
            rich.text.Text(""),
            rich.markdown.Markdown(ev.content, code_theme="monokai", hyperlinks=True),
            rich.text.Text(""),
            rich.rule.Rule(
                rich.text.Text(f"{fmt_tokens(ev.total_tokens)} tokens", style="dim"),
                style="dim cyan",
                align="right",
            ),
        ))

    def _show_warning(self, event: DisplayEvent[WarningEvent]) -> None:
        self._print(f"[bold yellow]warning[/bold yellow] / {event.payload.message}")

    def _show_error(self, event: DisplayEvent[ErrorEvent]) -> None:
        self._print(f"[bold red]error[/bold red] / {event.payload.message}")

    def _show_tool_result(self, event: DisplayEvent[ToolResultEvent]) -> None:
        ev = event.payload
        if isinstance(ev.result, dict) and "error" in ev.result:
            self._print(f"[bold red]tool error[/bold red] / {ev.result['error']}")

    def _show_info(self, event: DisplayEvent[InfoEvent]) -> None:
        self._print(f"[dim]info /[/dim] {event.payload.message}")

    def _show_html_info(self, event: DisplayEvent[HTMLInfoEvent]) -> None:
        ev = event.payload
        text = rich.markup.escape(ev.to_text())
        if ev.title:
            self._print(rich.panel.Panel(text, title=ev.title, border_style="cyan", box=rich.box.ROUNDED, padding=(0, 1)))
        else:
            self._print(f"[dim]info /[/dim] {text}")

    def _show_confirm(self, event: DisplayEvent[ConfirmEvent]) -> None:
        self._print(f"[dim]confirmed / {event.payload.source}: {event.payload.choice!r} for {event.payload.prompt!r}[/dim]")
    
    def _agent_bind(self, event: DisplayEvent[AgentBindEvent]) -> None:
        self._print(f"[dim]{rich.markup.escape(event.agent.name)} / attached[/dim]")
    
    def _agent_unbind(self, event: DisplayEvent[AgentUnbindEvent]) -> None:
        self._print(f"[dim]{rich.markup.escape(event.agent.name)} / detached[/dim]")

    def _unhandled(self, event: DisplayEvent) -> None:
        self._print(f"[yellow]unhandled[/yellow] / {rich.markup.escape(event.name)} (from {rich.markup.escape(event.agent.name)})")

    @staticmethod
    def _role_color(role: str) -> str:
        return {
            "system": "magenta",
            "user": "cyan",
            "assistant": "green",
            "tool": "yellow",
        }.get(role, "white")

    @staticmethod
    def _arg_str(args: JsonType) -> str:
        if isinstance(args, (str, int, float, bool, type(None))):
            return repr(args)
        if isinstance(args, list):
            return "[" + ", ".join(Display._arg_str(item) for item in args) + "]"
        assert isinstance(args, dict)
        pairs = []
        for k, v in args.items():
            if isinstance(v, str):
                v = ("'" + v[:47] + "...'") if len(v) > 50 else ("'" + v + "'")
            pairs.append(f"[bold yellow]{k}[/bold yellow]: {v}")
        return ", ".join(pairs)

def _rl_prompt(console: rich.console.Console, markup: str) -> str:
    """Render rich markup to an ANSI prompt for prompt_toolkit."""
    text = rich.text.Text.from_markup(markup)
    return "".join(
        (seg.style.render(seg.text) if seg.style else seg.text)
        for seg in console.render(text, options=console.options.update(no_wrap=True, justify=None))
        if isinstance(seg.text, str)
    ).rstrip("\n")
