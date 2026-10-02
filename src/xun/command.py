from __future__ import annotations
from typing import TYPE_CHECKING, Optional, Callable, Any, cast
import inspect
from .types import CancelledError
from .prompt import get_handoff_prompt
from .conversation_message import UserMessage
import datetime
from pathlib import Path
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
            agent.info(self.description_long)
        else:
            agent.info(self.description)

    def invoke(self, agent: "Agent[Agent.T.Init]", args: list[str] = []) -> None:
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
        subcommand.invoke(agent, args[1:])

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
    
    def with_fs_extra_defaults(self):
        """Register default commands with extra filesystem capabilities (when allow file read/write)."""
        self.register(*default_commands_with_fs())
        return self

def default_commands() -> list[Command]:
    from .compact import compact_conversation
    from .display_abstract import ShowExtensionsEvent, ShowHistoryEvent, ShowToolsEvent
    
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
                    

    def _revise_handler(agent: "Agent[Agent.T.Init]", args: list[str]) -> None:
        """Edit or roll back to the last user message.
        If no arguments are provided, the last user message is removed and execution pauses for re-entry.
        If arguments are provided, the last user message is replaced with the given content.
        Use '/continue' afterwards to proceed.
        Usage:
            revise
            revise <new content>
        """
        records = agent.conversation.pop_from_last_user_message()
        assert records and isinstance(records[0], UserMessage)
        if not args:
            agent.info("Revised to last user message.")
            return
        agent.instruct(" ".join(args))
        agent.info("Revised last user message. Use /continue to proceed.")

    def _retry_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.conversation.pop_from_last_user_message(inclusive=False)
        agent.info("Restarted from last user message.")

    def _config_handler(agent: "Agent[Agent.T.Init]", args: list[str]) -> None:
        """Show or edit the agent's configuration.
        If no arguments are provided, the current configuration is displayed.
        To edit a configuration value, provide a single argument in the format 'key.sub=value'.
        """
        if not args:
            agent.info(str(agent.config.to_json()))
            return
        
        if args:
            assert len(args) == 1, "Expected one argument for config edition. e.g. key.sub=value"
            key, value = args[0].split("=", 1)
            key_sp = key.split(".")
            var = agent.config
            for k in key_sp[:-1]:
                var = getattr(var, k)
            setattr(var, key_sp[-1], value)
            agent.info(f"Config updated: {key} = {getattr(var, key_sp[-1])!r}")

    def _tools_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.display_event(ShowToolsEvent.from_tools(agent.toolbox.list_tools()))

    def _compact_handler(agent: "Agent[Agent.T.Init]", args: list[str]) -> None:
        """Compact the conversation history.
        If 'toolcall' is provided as an argument, only tool call results are compacted.
        Usage: 
          /compact           # Compact the entire conversation history
          /compact toolcall  # Compact only tool call results
        """
        if not args:
            compact_conversation(agent)
            return

        if len(args) == 1:
            if args[0] == 'toolcall':
                reclaimed = agent.conversation.compact_toolcall()
                if reclaimed.reclaimed_count:
                    agent.info(f"Compacted {reclaimed.reclaimed_count} tool call result(s), reclaimed {reclaimed.reclaimed_fraction:.1%} of estimated message length.")
                else:
                    agent.info("No tool call results to compact.")
            else:
                raise ValueError(f"Unknown argument for compact: {args[0]}")
        else:
            raise ValueError(f"Unknown number of arguments for compact: {len(args)}")
    
    def _yolo_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.config.auto_confirm = not agent.config.auto_confirm
        agent.info(f"Auto-confirm is now {'enabled' if agent.config.auto_confirm else 'disabled'}.")

    def _history_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.display_event(ShowHistoryEvent(history=agent.conversation.to_history()))

    def _continue_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.execute()

    def _extensions_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.display_event(ShowExtensionsEvent.from_infos(agent.extension_loader.infos()))

    return [
        Command(name="tokens", description="Show tokens used in conversation.", handler=_token_query_handler),
        Command(name="clear", description="Clear conversation history. Use 'clear all' to also remove temporary files.", handler=_clear_handler),
        Command(name="continue", description="Continue execution.", handler=_continue_handler),
        Command(name="revise", handler=_revise_handler),
        Command(name="retry", description="Retry last message.", handler=_retry_handler),
        Command(name="config", handler=_config_handler),
        Command(name="tools", description="List registered tools.", handler=_tools_handler),
        Command(name="compact", handler=_compact_handler),
        Command(name="yolo", description="Toggle global auto approve (You Only Look Once).", handler=_yolo_handler),
        Command(name="history", description="Show history.", handler=_history_handler),
        Command(name="extensions", description="List discovered extensions with their status.", handler=_extensions_handler),
    ]

def default_commands_with_fs() -> list[Command]:
    from .store import Store

    def _save_handler(agent: "Agent[Agent.T.Init]") -> None:
        """Save current conversation history (experimental)."""
        store = Store()
        aim_dir = store.next_history_store()
        aim_dir.mkdir()
        agent.conversation.dump(aim_dir / "conversation.json")
        agent.info(f"Dumped to {aim_dir}")

    def _load_handler(agent: "Agent[Agent.T.Init]", idx: list[str]) -> None:
        """Load conversation history from save storage (experimental).
        It supports loading by index or the latest conversation. 
        e.g. 
            load latest
            load 3
        """
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
    
    def _render_handler(agent: "Agent[Agent.T.Init]", arguments: list[str]) -> None:
        """Render conversation history as HTML and save to a file.
        Usage: 
            render <file_path>
        """
        if not arguments:
            agent.error("Please provide a file path to save the rendered HTML.")
            return
        assert len(arguments) == 1, "Please provide exactly one file path to save the rendered HTML."
        aim_path_raw = arguments[0]
        if not aim_path_raw.endswith(".html"):
            aim_path_raw += ".html"
        aim_path = agent.workspace.resolve(aim_path_raw, raise_on_invalid=True).path

        html = agent.display.render_history_as_html(title=f"xun · {agent.name}")
        aim_path.write_text(html, encoding="utf-8")
    
    def _handoff(agent: "Agent[Agent.T.Init]", arguments: list[str]) -> None:
        """Prepare for handoff by packaging the current task into a zip file.
        If no file name is given, defaults to handoff_<timestamp>.zip.
        Usage:
            handoff [file_name]
        """
        if not arguments:
            file_path = f"handoff_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.zip"
        else:
            assert len(arguments) == 1, "Please provide exactly one file name for the handoff package."
            file_path = arguments[0]
        resolved = agent.workspace.resolve(Path(file_path), raise_on_invalid=True)
        if not resolved.in_workdir:
            raise ValueError("The resolved path is not within the workspace directory.")
        rel_path = resolved.path.resolve().relative_to(agent.workspace.workdir)
        prompt = get_handoff_prompt(str(rel_path))
        agent.instruct(prompt, _emit_event=False).execute()

    return [
        Command(name="render", handler=_render_handler),
        Command(name="save", handler=_save_handler),
        Command(name="load", handler=_load_handler),
        Command(name="handoff", handler=_handoff)
    ]