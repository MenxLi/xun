import hashlib, datetime
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


IMAGE_PREFIX = "image:"

def _parse_image_block(image_block: str) -> list[str] | None:
    images: list[str] = []
    for token in shlex.split(image_block):
        if not token.startswith(IMAGE_PREFIX) or len(token) <= len(IMAGE_PREFIX):
            return None
        images.append(token[len(IMAGE_PREFIX):])
    return images or None


def _parse_message_input(raw_input: str) -> MessageInstruction:
    content = raw_input.strip()
    if not content.startswith("["):
        return MessageInstruction(content=raw_input)

    image_block_end = content.find("]")
    if image_block_end < 0:
        raise ValueError("Invalid image attachment syntax: missing closing ']'.")

    image_block = content[1:image_block_end].strip()
    images = _parse_image_block(image_block)
    if images is None:
        return MessageInstruction(content=raw_input)

    return MessageInstruction(
        content=content[image_block_end + 1:].strip(),
        images=images,
    )


def input_to_instruction(raw_input: str) -> Instruction:
    if raw_input.startswith("."):
        raw_command = raw_input[1:].strip()
        command = raw_command.split()[0] if raw_command else ""
        args = shlex.split(raw_command)[1:] if raw_command else []
        return CommandInstruction(command=command, args=args)
    return _parse_message_input(raw_input)


class Display(DisplayAbstract):

    def __init__(self):
        self.console = rich.console.Console()
        self.lock = threading.Lock()

    def _print(self, *args, **kwargs):
        with self.lock:
            if isinstance(args[0] if args else None, str):
                self.console.print(f"[dim][{datetime.datetime.now().strftime('%H:%M:%S')}][/dim]", end=" ")
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

    def on_event(self, event: DisplayEvent):
        match event.event:
            case ShowHelpEvent():
                self.__on_show_help(event)
            case ShowHistoryEvent():
                self.__on_show_history(event)
            case ToolCallEvent():
                self.__on_tool_call(event)
            case ModelWorkingEvent():
                self.__on_model_working(event)
            case ModelMessageEvent():
                self.__on_model_message(event)
            case ErrorEvent():
                self.__on_error(event)
            case ToolResultEvent():
                self.__on_tool_result(event)
            case InfoEvent():
                self.__on_info(event)
            case _:
                self.__on_unhandled(event)

    # ── Private dispatch handlers ──────────────────────────────────────

    def __on_show_help(self, event: DisplayEvent[ShowHelpEvent]) -> None:
        self._print(
            rich.panel.Panel.fit(
                event.event.message,
                title="[bold blue]Help[/bold blue]",
                border_style="green",
            )
        )

    def __on_show_history(self, event: DisplayEvent[ShowHistoryEvent]) -> None:
        history: list[Conversation.MessageRecord] = event.event.history

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
            color = self.__role_color(record["role"])
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

    def __on_tool_call(self, event: DisplayEvent[ToolCallEvent]) -> None:
        assert event.tool_call_context is not None, "ToolCallEvent must have been emitted within a tool call context"
        ev: ToolCallEvent = event.event
        tool_call_sha = hashlib.sha1(ev.tool_call_id.encode()).hexdigest()[:6]
        leading = f":wrench: {event.tool_call_context.agent.name} [dim]{tool_call_sha}[/dim]"
        self._print(f"{leading} [bold green]{ev.tool_name}[/bold green]({self.__arg_str(ev.args)})")

    def __on_model_working(self, event: DisplayEvent[ModelWorkingEvent]) -> None:
        assert event.execution_context is not None, "ModelWorkingEvent must have been emitted within an execution context"
        ev: ModelWorkingEvent = event.event
        self._print(
            f":green_circle: {event.execution_context.agent.name} running. " +
            (f"(max remaining iterations: {ev.remaining_iterations})" if ev.remaining_iterations is not None and ev.remaining_iterations < 8 else "")
        )

    def __on_model_message(self, event: DisplayEvent[ModelMessageEvent]) -> None:
        assert event.execution_context is not None, "ModelMessageEvent must have been emitted within an execution context"
        ev: ModelMessageEvent = event.event
        self._print(
            rich.panel.Panel(
                rich.markdown.Markdown(
                    ev.content,
                    code_theme="monokai",
                    hyperlinks=True,
                ),
                title=f"[bold blue]{event.execution_context.agent.name}[/bold blue]",
                border_style="blue",
            ),
        )

    def __on_error(self, event: DisplayEvent[ErrorEvent]) -> None:
        self._print(f":red_circle: {event.event.message}")

    def __on_tool_result(self, event: DisplayEvent[ToolResultEvent]) -> None:
        ev: ToolResultEvent = event.event
        if isinstance(ev.result, dict) and "error" in ev.result:
            self._print(f":red_circle: tool error: {ev.result['error']}")

    def __on_info(self, event: DisplayEvent[InfoEvent]) -> None:
        self._print(f":information_source: {event.event.message}")

    def __on_unhandled(self, event: DisplayEvent) -> None:
        self._print(f":question: Unhandled event: {event}")

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def __role_color(role: str) -> str:
        match role:
            case "system": return "magenta"
            case "user": return "cyan"
            case "assistant": return "green"
            case "tool": return "yellow"
            case _: return "white"

    @staticmethod
    def __arg_str(args: JsonType) -> str:
        if isinstance(args, (str, int, float, bool, type(None))):
            return repr(args)
        if isinstance(args, list):
            return "[" + ", ".join(Display.__arg_str(item) for item in args) + "]"
        assert isinstance(args, dict)
        s = []
        for k, v in args.items():
            if isinstance(v, str):
                v = ("'" + v[:47] + "...'") if len(v) > 50 else ("'" + v + "'")
            s.append(f"[bold yellow]{k}[/bold yellow]: {v}")
        return ", ".join(s)


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
