import hashlib
from selectors import DefaultSelector, EVENT_READ

# import for arrow key support in input()
import readline     # noqa
import sys, time, threading
import shlex
import rich
import rich.box
import rich.table
import rich.console
import rich.prompt
import rich.panel
import rich.markdown

from .display_abstract import *
from .config import app_config

REPL_HELP_MSG = """\
[bold cyan]Available commands:[/bold cyan]
[bold yellow].help[/bold yellow] - Show this help message
[bold yellow].restart[/bold yellow] - Clear conversation history and restart the agent
[bold yellow].retry[/bold yellow] - Retry the last user message (clear to last user message)
[bold yellow].revise[/bold yellow] - Re-input the last user message (clear to last user message)
[bold yellow].tools[/bold yellow] - List registered tools
[bold yellow].config[/bold yellow] - Show current configuration
[bold yellow].condense[/bold yellow] - Condense conversation history to reduce token usage
[bold yellow].dump[/bold yellow] - Dump conversation history to a store
[bold yellow].load[/bold yellow] - Load conversation history from latest store or specified store
[bold yellow].history[/bold yellow] - Show conversation history in the terminal
[bold yellow].exit[/bold yellow] - Exit the program\
"""


def input_to_instruction(raw_input: str) -> Instruction:
    if raw_input.startswith("."):
        raw_command = raw_input[1:].strip()
        command = raw_command.split()[0] if raw_command else ""
        args = shlex.split(raw_command)[1:] if raw_command else []
        return CommandInstruction(command=command, args=args)
    return MessageInstruction(content=raw_input)

class Display(DisplayAbstract):

    def __init__(self):
        self.console = rich.console.Console()
        self.lock = threading.Lock()

    def _print(self, *args, **kwargs):
        with self.lock:
            self.console.print(*args, **kwargs)
    
    def get_instruction(self) -> Instruction:
        while True:
            self._print("[gray]Input (`.help` to show help message).[/gray]")
            with self.lock:
                raw_input = input(">>> ").strip()
            if raw_input:
                return input_to_instruction(raw_input)
    
    def get_confirm(
        self,
        prompt: str,
        message: Optional[str] = None,
        title: Optional[str] = None,
        subtitle: str | None = None,
        default: bool = True, 
        ) -> bool:
        with self.lock:
            if message:
                _note(self.console, message, title, subtitle)
            return _confirm(self.console, prompt, default)

    def handle(self, event: DisplayEvent):
        match event.event:
            case ShowHelpEvent():
                self._print(
                    rich.panel.Panel.fit(
                        REPL_HELP_MSG,
                        title="[bold blue]Help[/bold blue]",
                        border_style="green",
                    )
                )

            case ShowHistoryEvent(history=history):
                def role_color(role: str) -> str:
                    if role == "system": return "magenta"
                    elif role == "user": return "cyan"
                    elif role == "assistant": return "green"
                    elif role == "tool": return "yellow"
                    else: return "white"

                if not history:
                    self._print(
                        rich.panel.Panel(
                            "[dim]No conversation history yet.[/dim]",
                            title="[bold blue]Conversation History[/bold blue]",
                            border_style="green",
                            box=rich.box.ROUNDED,
                            padding=(0, 1),
                        )
                    )
                    return

                sub_panels: list[rich.panel.Panel] = []
                counter = 0
                for record in history:
                    if not record['content']:
                        continue
                    counter += 1
                    color = role_color(record["role"])
                    row = rich.table.Table.grid(expand=True)
                    row.add_column(style=f"bold {color}", width=10)
                    row.add_column(ratio=1)
                    row.add_row(
                        record["role"],
                        rich.markdown.Markdown(
                            record["content"],
                            code_theme="monokai",
                            hyperlinks=True,
                        ),
                    )
                    sub_panels.append(
                        rich.panel.Panel(
                            row,
                            border_style=color,
                            box=rich.box.ROUNDED,
                            padding=(0, 0),
                        )
                    )

                self._print(
                    rich.panel.Panel(
                        rich.console.Group(*sub_panels),
                        title="[bold blue]Conversation History[/bold blue]",
                        subtitle=f"[dim]{counter} msgs[/dim]",
                        box=rich.box.ROUNDED,
                        padding=(0, 1),
                    )
                )
            
            case ToolCallEvent(tool_name=tool_name, args=arguments, tool_call_id=tool_call_id):
                assert event.tool_call_context is not None, "ToolCallEvent must have been emitted within a tool call context"
                def arg_str(args: JsonType) -> str:
                    if isinstance(args, (str, int, float, bool, type(None))):
                        return repr(args)
                    elif isinstance(args, list):
                        return "[" + ", ".join(arg_str(item) for item in args) + "]"
                    assert isinstance(args, dict)

                    s = []
                    for k, v in args.items():
                        if isinstance(v, str):
                            if len(v) > 50:
                                v = v[:47] + "..."
                            v = "\'" + v + "\'"
                        s.append(f"[bold yellow]{k}[/bold yellow]: {v}")
                    return ", ".join(s)
                tool_call_sha = hashlib.sha1(tool_call_id.encode()).hexdigest()[:6]
                leading = f":wrench: {event.tool_call_context.agent.name} [dim]{tool_call_sha}[/dim]"
                self._print(f"{leading} [bold green]{tool_name}[/bold green]({arg_str(arguments)})")

            case ModelWorkingEvent(remaining_iterations=remaining_iterations):
                assert event.execution_context is not None, "ModelWorkingEvent must have been emitted within an execution context"
                self._print(
                    f":green_circle: {event.execution_context.agent.name} running. " + 
                    (f"(max remaining iterations: {remaining_iterations})" if remaining_iterations is not None and remaining_iterations < 8 else "")
                    )
            
            case ModelMessageEvent(content=message):
                assert event.execution_context is not None, "ModelMessageEvent must have been emitted within an execution context"
                self._print(
                    rich.panel.Panel(
                        rich.markdown.Markdown(
                            message, 
                            code_theme="monokai",
                            hyperlinks=True,
                        ),
                        title=f"[bold blue]{event.execution_context.agent.name}[/bold blue]",
                        border_style="blue",
                    ), 
                )

            case ErrorEvent(message=message):
                self._print(f":red_circle: {message}")

            case ToolResultEvent(result=result):
                if isinstance(result, dict) and "error" in result:
                    self._print(f":red_circle: tool error: {result['error']}")

            case InfoEvent(message=message):
                self._print(f":information_source: {message}")

            case _:
                self._print(f":question: Unhandled event: {event}")

def _confirm(console: rich.console.Console, prompt: str, default: bool = False) -> bool:

    cfg = app_config()
    if not cfg.auto_confirm:
        ret = rich.prompt.Confirm.ask(prompt, default=default)
        console.print()  # add a newline after the prompt
        return ret
    else:
        if cfg.auto_confirm_timeout <= 0 or not sys.stdin.isatty():
            return default

        def parse_confirmation(response: str) -> bool | None:
            normalized = response.strip().lower()
            if normalized == "":
                return default
            if normalized in {"y", "yes"}:
                return True
            if normalized in {"n", "no"}:
                return False
            return None

        selector = DefaultSelector()
        try:
            selector.register(sys.stdin, EVENT_READ)
        except (ValueError, OSError, PermissionError):
            return default

        deadline = time.monotonic() + cfg.auto_confirm_timeout
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            while True:
                remaining = deadline - time.monotonic() + 0.01
                if remaining <= 0:
                    console.print()
                    return default

                console.print(
                    f"{prompt} {suffix} (auto-confirming in {max(1, int(remaining))} seconds): ",
                    end="",
                    markup=False,
                    soft_wrap=True,
                )
                if not selector.select(remaining):
                    console.print()
                    return default

                response = sys.stdin.readline()
                if response == "":
                    console.print()
                    return default

                approved = parse_confirmation(response)
                if approved is not None:
                    return approved

                console.print("[prompt.invalid]Please enter Y or N[/prompt.invalid]")
        finally:
            selector.close()

def _note(console: rich.console.Console, message: str, title: Optional[str] = "Note", subtitle: Optional[str] = None) -> None:
    panel = rich.panel.Panel(
        message, border_style="yellow", 
        title=f"[bold yellow]{title}[/bold yellow]" if title else None,
        subtitle=f"[dim]{subtitle}[/dim]" if subtitle else None,
        )
    console.print(panel)
