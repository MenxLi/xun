"""The agent's built-in slash commands and their registration.

The registry mechanism itself lives in `xun.core.command`; the default
command *content* belongs to this layer (it is app policy, not engine
policy) and is the sole consumer of the `retry` return-value signal.
"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.command import Command, CommandRegistry
from ..core.compact import compact_conversation
from ..core.conversation_message import UserMessage
from ..core.display_abstract import ShowExtensionsEvent, ShowHistoryEvent, ShowToolsEvent
from ..core.prompt import get_handoff_prompt

if TYPE_CHECKING:
    from ..core.agent import Agent


def default_commands() -> list[Command]:
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
                agent.display_info(f"Tokens used in conversation: {token:.2f}{token_str_unit}")
            else:
                agent.display_info(f"Tokens used in conversation: {token}")
        else:
            agent.display_info("Tokens used in conversation: Unknown (not yet calculated)")

    def _clear_handler(agent: "Agent[Agent.T.Init]", args: list[str]) -> None:
        agent.conversation.clear()
        agent.display_info("History cleared.")
        if len(args) > 0 and args[0] == "all":
            # remove everything from the tempdir as well
            if agent.workspace.tempdir is not None:
                if (tmp_dir := agent.workspace.tempdir.exist_path) is not None:
                    for item in tmp_dir.iterdir():
                        if item.is_file():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(item)
                    agent.display_info("Temporary files cleared.")
                else:
                    agent.display_info("No temporary files to clear.")
                    

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
            agent.display_info("Revised to last user message.")
            return
        agent.instruct(" ".join(args))
        agent.display_info("Revised last user message. Use /continue to proceed.")

    def _retry_handler(agent: "Agent[Agent.T.Init]") -> bool:
        """Returns True so the dispatch layer performs the follow-up execution."""
        agent.conversation.pop_from_last_user_message(inclusive=False)
        agent.display_info("Restarted from last user message.")
        return True

    def _config_handler(agent: "Agent[Agent.T.Init]", args: list[str]) -> None:
        """Show or edit the agent's configuration.
        If no arguments are provided, the current configuration is displayed.
        To edit a configuration value, provide a single argument in the format 'key.sub=value'.
        """
        if not args:
            agent.display_info(agent.config.to_json_str())
            return
        
        if args:
            assert len(args) == 1, "Expected one argument for config edition. e.g. key.sub=value"
            key, value = args[0].split("=", 1)
            key_sp = key.split(".")
            var = agent.config
            for k in key_sp[:-1]:
                var = getattr(var, k)
            setattr(var, key_sp[-1], value)
            agent.display_info(f"Config updated: {key} = {getattr(var, key_sp[-1])!r}")

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
                    agent.display_info(f"Compacted {reclaimed.reclaimed_count} tool call result(s), reclaimed {reclaimed.reclaimed_fraction:.1%} of estimated message length.")
                else:
                    agent.display_info("No tool call results to compact.")
            else:
                raise ValueError(f"Unknown argument for compact: {args[0]}")
        else:
            raise ValueError(f"Unknown number of arguments for compact: {len(args)}")
    
    def _yolo_handler(agent: "Agent[Agent.T.Init]") -> None:
        agent.config.auto_confirm = not agent.config.auto_confirm
        agent.display_info(f"Auto-confirm is now {'enabled' if agent.config.auto_confirm else 'disabled'}.")

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
    def _render_handler(agent: "Agent[Agent.T.Init]", arguments: list[str]) -> None:
        """Render conversation history as HTML and save to a file.
        Usage: 
            render <file_path>
        """
        if not arguments:
            agent.display_error("Please provide a file path to save the rendered HTML.")
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
        Command(name="handoff", handler=_handoff)
    ]


def register_default_commands(
    registry: CommandRegistry,
    *extra: Command,
    fs_extra: bool = False,
) -> CommandRegistry:
    """Install the default commands (plus FS extras when file access is allowed)."""
    registry.register(*default_commands(), *extra)
    if fs_extra:
        registry.register(*default_commands_with_fs())
    return registry
