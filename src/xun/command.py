from __future__ import annotations
from typing import TYPE_CHECKING, Optional, Callable, Any
import inspect
import shlex
from .types import CancelledError
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

    def __init__(
        self,
        name: str,
        handler: CommandHandler | "CommandRegistry",
        description: str = "",
        description_long: Optional[str] = None,
    ) -> None:
        self.name = name

        if isinstance(handler, CommandRegistry):
            self._run = lambda agent, args: self._dispatch(agent, handler, args)  # type: ignore[misc]
        else:
            n_args_accepted = len(inspect.signature(handler).parameters)
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
            agent.info(self.description_long)
        else:
            agent.info(self.description)

    def invoke(self, agent: "Agent[Agent.T.Init]", arguments: Optional[str] = None) -> None:
        args = shlex.split(arguments) if arguments else []
        self._invoke_args(agent, args)

    def _invoke_args(self, agent: "Agent[Agent.T.Init]", args: list[str]) -> None:
        if args in (["-h"], ["--help"]):
            self.show_help(agent)
            return
        try:
            self._run(agent, args)
        except (KeyboardInterrupt, CancelledError):
            raise
        except Exception as e:
            agent.error(f"Error executing command '{self.name}': {e}")

    def _dispatch(self, agent: "Agent[Agent.T.Init]", registry: "CommandRegistry", args: list[str]) -> None:
        if not args:
            raise ValueError(
                f"Subcommand is required. Use '-h' for help.\n"
                f"{self._format_subcommands(registry)}"
            )
        subcommand = registry.get(args[0])
        if subcommand is None:
            raise ValueError(f"Unknown subcommand: {args[0]}")
        subcommand._invoke_args(agent, args[1:])

class CommandRegistry:
    def __init__(self):
        self.commands: dict[str, Command] = {}
    
    def clone(self):
        new_registry = CommandRegistry()
        new_registry.commands = self.commands.copy()
        return new_registry

    def register(self, *commands: Command | CommandHandler):
        for command in commands:
            if isinstance(command, Command):
                assert command.name != 'help', "Command name 'help' is reserved."
                self.commands[command.name] = command
            elif callable(command):
                cmd = Command.from_function(command)
                assert cmd.name != 'help', "Command name 'help' is reserved."
                self.commands[cmd.name] = cmd
            else:
                raise TypeError(f"Expected Command or callable")
        return self

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

    def with_defaults(self):
        self.register(*default_commands())
        return self

def default_commands() -> list[Command]:
    from .store import Store
    from .display_abstract import ShowHistoryEvent, ShowToolsEvent
    
    def _token_query_handler(agent: "Agent[Agent.T.Init]") -> None:
        token = agent.conversation.total_tokens
        if token is not None:
            token_str_unit = ""
            if token >= 1000:
                token_str_unit = "K"
                token /= 1000
            if token >= 1000:
                token_str_unit = "M"
                token /= 1000

            if token_str_unit:
                agent.info(f"Tokens used in conversation: {token:.2f}{token_str_unit}")
            else:
                agent.info(f"Tokens used in conversation: {token}")
        else:
            agent.info("Tokens used in conversation: Unknown (not yet calculated)")

    def _clear_handler(agent: "Agent[Agent.T.Init]", args: list[str]) -> None:
        agent.conversation.clear()
        agent.info("History cleared.")
        if len(args) > 0 and args[0] == "all":
            # remove everything from the tempdir as well
            if agent.workspace.tempdir is not None:
                if (tmp_dir := agent.workspace.tempdir.exist_path) is not None:
                    for item in tmp_dir.iterdir():
                        if item.is_file():
                            item.unlink()
                        elif item.is_dir():
                            import shutil
                            shutil.rmtree(item)
                    agent.info("Temporary files cleared.")
                else:
                    agent.info("No temporary files to clear.")
                    

    def _revise_handler(agent: "Agent[Agent.T.Init]") -> None:
        records = agent.conversation.pop_from_last_user_message()
        assert records and isinstance(records, list) and len(records) > 0 and isinstance(records[0], dict) and records[0].get("role") == "user"
        agent.info("Revised to last user message.")

    def _retry_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.conversation.pop_from_last_user_message(inclusive=False)
        agent.info("Restarted from last user message.")

    def _config_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.info(str(agent.config.to_json()))

    def _tools_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.display_event(ShowToolsEvent.from_tools(agent.toolbox.list_tools()))

    def _save_handler(agent: "Agent[Agent.T.Init]") -> None:
        store = Store()
        aim_dir = store.next_history_store()
        aim_dir.mkdir()
        agent.conversation.dump(aim_dir / "conversation.json")
        agent.info(f"Dumped to {aim_dir}")

    def _load_handler(agent: "Agent[Agent.T.Init]", idx: list[str]) -> None:
        store = Store()
        if not idx:
            agent.error("Please provide an index or 'latest' to load history.")
            return
        target = idx[0]
        if target.isdigit():
            aim_dir = store.get_history_store(target)
            if not aim_dir:
                agent.error(f"History {target} not found.")
                return
        elif target == "latest":
            latest_dir = store.latest_history_store()
            if latest_dir is None:
                agent.info("No history found.")
                return
            aim_dir = latest_dir
        else:
            agent.error(f"Invalid index '{target}'. Use a number or 'latest'.")
            return
        conv_file = aim_dir / "conversation.json"
        if not conv_file.exists():
            agent.error(f"No conversation history found in {conv_file}.")
            return
        agent.conversation.load(conv_file)
        agent.info(f"Loaded from {aim_dir}")

    def _condense_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.condense_conversation()
    
    def _yolo_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.config.auto_confirm = not agent.config.auto_confirm
        agent.info(f"Auto-confirm is now {'enabled' if agent.config.auto_confirm else 'disabled'}.")

    def _history_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.display_event(ShowHistoryEvent(history=agent.conversation.to_history()))

    def _continue_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.execute()

    return [
        Command(name="tokens", description="Show tokens used in conversation.", handler=_token_query_handler),
        Command(name="clear", description="Clear conversation history. Use 'clear all' to also remove temporary files.", handler=_clear_handler),
        Command(name="continue", description="Continue execution.", handler=_continue_handler),
        Command(name="revise", description="Edit last message.", handler=_revise_handler),
        Command(name="retry", description="Retry last message.", handler=_retry_handler),
        Command(name="config", description="Show configuration.", handler=_config_handler),
        Command(name="tools", description="List registered tools.", handler=_tools_handler),
        Command(name="save", description="Save history.", handler=_save_handler),
        Command(name="load", description="Load history. (latest, [idx])", handler=_load_handler),
        Command(name="compact", description="Condense conversation.", handler=_condense_handler),
        Command(name="yolo", description="Toggle auto-confirm (auto-approve actions without prompting).", handler=_yolo_handler),
        Command(name="history", description="Show history.", handler=_history_handler),
    ]