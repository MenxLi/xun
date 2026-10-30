from __future__ import annotations
from typing import TYPE_CHECKING, Optional, Callable, Any, cast
import inspect
from .types import CancelledError
from .config import DEFAULTS
import rich
if TYPE_CHECKING:
    from .agent import Agent

type CommandHandlerWArgs = Callable[["Agent[Agent.T.Init]", list[str]], Any]
type CommandHandlerWOArgs = Callable[["Agent[Agent.T.Init]"], Any]
type CommandHandler = CommandHandlerWArgs | CommandHandlerWOArgs

class Command:
    _run: CommandHandlerWArgs
    name: str
    description: str
    description_long: Optional[str]
    override: bool

    def __init__(
        self,
        name: str,
        handler: CommandHandler | "CommandRegistry",
        description: str = "",
        description_long: Optional[str] = None,
        override: bool = False,
    ) -> None:
        self.name = name
        self.override = override

        if isinstance(handler, CommandRegistry):
            self._run = lambda agent, args: self._dispatch(agent, handler, args)  # type: ignore[misc]
        else:
            n_args_accepted = len(inspect.signature(cast("Callable[..., Any]", handler)).parameters)
            if n_args_accepted == 1:
                self._run = lambda agent, _arguments=None: handler(agent)  # type: ignore[misc]
            elif n_args_accepted == 2:
                self._run = lambda agent, arguments=None: handler(agent, arguments)  # type: ignore[misc]
            else:
                raise TypeError(f"Command handler must accept 1 or 2 args, got {n_args_accepted}")

        if not description:
            if isinstance(handler, CommandRegistry):
                func_doc = "No description provided."
            else:
                func_doc = (handler.__doc__ or "").strip() or "No description provided."
            description = func_doc.splitlines()[0]  # Use the first line of the docstring as the description

            if not description_long:
                description_long = func_doc if len(func_doc.splitlines()) > 1 else None

            if isinstance(handler, CommandRegistry) and not description_long:
                description_long = self._format_subcommands(handler)

        self.description = description
        self.description_long = description_long

    @staticmethod
    def _format_subcommands(registry: "CommandRegistry") -> str:
        lines = ["Subcommands:"]
        for cmd in registry.commands.values():
            lines.append(f"  {cmd.name:<24}{cmd.description}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Command(name={self.name!r}, description={self.description!r})"
    
    @staticmethod
    def from_function(func: CommandHandler) -> Command:
        return Command(
            name=func.__name__,
            handler=func
        )

    def show_help(self, agent: "Agent[Agent.T.Init]") -> None:
        if self.description_long:
            agent.display_info(self.description_long)
        else:
            agent.display_info(self.description)

    def invoke(self, agent: "Agent[Agent.T.Init]", args: list[str] = []) -> Any:
        if args in (["-h"], ["--help"]):
            self.show_help(agent)
            return None
        try:
            return self._run(agent, args)
        except (KeyboardInterrupt, CancelledError):
            raise
        except Exception as e:
            agent.display_error(f"Error executing command '{self.name}': {e}")

    def _dispatch(self, agent: "Agent[Agent.T.Init]", registry: "CommandRegistry", args: list[str]) -> Any:
        if not args:
            raise ValueError(
                f"Subcommand is required. Use '-h' for help.\n"
                f"{self._format_subcommands(registry)}"
            )
        subcommand = registry.get(args[0])
        if subcommand is None:
            raise ValueError(f"Unknown subcommand: {args[0]}")
        return subcommand.invoke(agent, args[1:])

class CommandRegistry:
    def __init__(self):
        self.commands: dict[str, Command] = {}
    
    def clone(self):
        new_registry = CommandRegistry()
        new_registry.commands = self.commands.copy()
        return new_registry

    def register(self, *commands: Command | CommandHandler, raise_on_conflict: bool = False):
        for command in commands:
            if isinstance(command, Command):
                assert command.name != 'help', "Command name 'help' is reserved."
                self._place(command, raise_on_conflict=raise_on_conflict)
            elif callable(command):
                cmd = Command.from_function(command)
                assert cmd.name != 'help', "Command name 'help' is reserved."
                self._place(cmd, raise_on_conflict=raise_on_conflict)
            else:
                raise TypeError(f"Expected Command or callable")
        return self

    def _place(self, command: Command, raise_on_conflict) -> None:
        old = self.commands.get(command.name)
        if old is None:
            self.commands[command.name] = command
            return
        if command.override:
            self.commands[command.name] = command
            msg = f"Overriding command '{command.name}'"
        elif old.override:
            msg = f"Kept existing override for '{command.name}', skipped incoming"
        else:
            if raise_on_conflict:
                raise ValueError(f"Conflict command name: {command.name}")
            msg = f"Skipped existing command '{command.name}'"
        if DEFAULTS.info_command_override:
            rich.print(f"[blue]Info: {msg}[/blue]")

    def get(self, name: str) -> Optional[Command]:
        from .display_abstract import ShowHelpEvent
        if name == 'help':
            return Command(
                name='help',
                description='Show this help message.',
                handler=lambda agent: agent.display_event(
                    ShowHelpEvent.from_commands(tuple(self.commands.values()))
                )
            )
        return self.commands.get(name)
