import hashlib, datetime, re
import readline     # noqa
import threading
import rich
import rich.box
import rich.table
import rich.console
import rich.panel
import rich.markdown
import rich.markup
import rich.text

from ..display_abstract import *

class Display(DisplayAbstract):
    def __init__(self):
        self.console = rich.console.Console()
        self.lock = threading.Lock()

    def _print(self, *args, **kwargs):
        with self.lock:
            if isinstance(args[0] if args else None, str):
                self.console.print(f"[dim][{datetime.datetime.now().strftime('%H:%M:%S')}][/dim]", end=" ")
            self.console.print(*args, **kwargs)

    def get_choice(self, request: DisplayAbstract.ChoiceRequest) -> str:
        choices = request.choices
        choices_str = "\n".join(f"  [{i}] {c}" for i, c in enumerate(choices, start=1))
        extra_choice_idx = len(choices) + 1 if request.allow_extra else None
        if request.allow_extra:
            choices_str += f"\n  [{extra_choice_idx}] Other (enter your own choice)"
        full_msg = f"{request.message}\n--- Choices ---\n{choices_str}"
        default_idx = choices.index(request.default) + 1 if request.default in choices else None
        with self.lock:
            if request.message:
                _note(self.console, full_msg, request.title, request.subtitle)
            choice_idx = _choose_from_int(
                self.console, 
                prompt = request.prompt, 
                n_choices=len(choices) + (1 if request.allow_extra else 0),
                default=default_idx)
            if request.allow_extra and choice_idx == extra_choice_idx:
                extra_choice = _ask_text(self.console, "Enter your choice")
                return extra_choice
            return choices[choice_idx - 1]
        

    def on_event(self, event: DisplayEvent):
        match event.payload:
            case ShowHelpEvent(): self._show_help(event)
            case ShowToolsEvent(): self._show_tools(event)
            case ShowHistoryEvent(): self._show_history(event)
            case ToolCallEvent(): self._show_tool_call(event)
            case ModelWorkingEvent(): self._show_model_working(event)
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
        self._print(f":wrench: {event.agent.name} [dim]{tool_id}[/dim] [bold green]{ev.tool_name}[/bold green]({self._arg_str(ev.args)})")

    def _show_model_working(self, event: DisplayEvent[ModelWorkingEvent]) -> None:
        ev = event.payload
        msg = f":green_circle: {event.agent.name} running"
        if ev.remaining_iterations and ev.remaining_iterations < 8:
            msg += f" (max {ev.remaining_iterations})"
        self._print(msg)

    def _show_model_message(self, event: DisplayEvent[ModelMessageEvent]) -> None:
        ev = event.payload
        if ev.content.strip():
            self._print(rich.panel.Panel(rich.markdown.Markdown(ev.content, code_theme="monokai", hyperlinks=True), title=f" {event.agent.name} ", border_style="blue"))

    def _show_warning(self, event: DisplayEvent[WarningEvent]) -> None:
        self._print(f":yellow_circle: {event.payload.message}")

    def _show_error(self, event: DisplayEvent[ErrorEvent]) -> None:
        self._print(f":red_circle: {event.payload.message}")

    def _show_tool_result(self, event: DisplayEvent[ToolResultEvent]) -> None:
        ev = event.payload
        if isinstance(ev.result, dict) and "error" in ev.result:
            self._print(f":red_circle: tool error: {ev.result['error']}")

    def _show_info(self, event: DisplayEvent[InfoEvent]) -> None:
        self._print(f":information_source: {event.payload.message}")

    def _show_html_info(self, event: DisplayEvent[HTMLInfoEvent]) -> None:
        ev = event.payload
        text = rich.markup.escape(ev.to_text())
        if ev.title:
            self._print(rich.panel.Panel(text, title=ev.title, border_style="cyan", box=rich.box.ROUNDED, padding=(0, 1)))
        else:
            self._print(f":information_source: {text}")

    def _show_confirm(self, event: DisplayEvent[ConfirmEvent]) -> None:
        if event.payload.source == "user":
            return
        self._print(f"[dim]Confirmed by {event.payload.source}: {event.payload.choice!r} for {event.payload.prompt!r}[/dim]")
    
    def _agent_bind(self, event: DisplayEvent[AgentBindEvent]) -> None:
        self._print(f":glowing_star: {event.agent.name} attached.")
    
    def _agent_unbind(self, event: DisplayEvent[AgentUnbindEvent]) -> None:
        self._print(f":waving_hand: {event.agent.name} detached.")

    def _unhandled(self, event: DisplayEvent) -> None:
        self._print(f":question: Unhandled {event.name} (from {event.agent.name})")

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

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def _rl_prompt(console: rich.console.Console, markup: str) -> str:
    """Render markup to an ANSI prompt for `input()`; escapes wrapped in \\001..\\002 so readline ignores their width."""
    text = rich.text.Text.from_markup(markup)
    rendered = "".join(
        (seg.style.render(seg.text) if seg.style else seg.text)
        for seg in console.render(text, options=console.options.update(no_wrap=True, justify=None))
        if isinstance(seg.text, str)
    ).rstrip("\n")
    return _ANSI_RE.sub(lambda m: "\x01" + m.group(0) + "\x02", rendered)

def _ask_text(console: rich.console.Console, prompt: str) -> str:
    return input(_rl_prompt(console, prompt + " "))

def _choose_from_int(
    console: rich.console.Console, 
    prompt: str, 
    n_choices: int,
    default: Optional[int] = None,
    ) -> int:
    if default is None:
        default = 1
    valid = {str(i) for i in range(1, n_choices + 1)}
    choices_str = "/".join(sorted(valid, key=int))
    prompt_str = _rl_prompt(console, f"{prompt} [bold magenta][{choices_str}][/bold magenta] [bold cyan]({default})[/bold cyan]: ")
    while True:
        answer = input(prompt_str).strip() or str(default)
        if answer in valid:
            break
        console.print("[red]Please select one of the available options[/red]")
    console.print()
    return int(answer)

def _note(console: rich.console.Console, message: str, title: Optional[str] = "Note", subtitle: Optional[str] = None) -> None:
    panel = rich.panel.Panel(message, border_style="yellow", title=f"[bold yellow]{title}[/bold yellow]" if title else None, subtitle=f"[dim]{subtitle}[/dim]" if subtitle else None)
    console.print(panel)
