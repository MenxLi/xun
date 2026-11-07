"""Public API. Heavy display/CLI/web symbols resolve lazily on first access
(PEP 562), so `import xun` never loads fastapi, uvicorn, docker, etc."""

from typing import TYPE_CHECKING

from .core.display_abstract import DisplayAbstract
from .core.null_display import NullDisplay
from .core.types import Result, ToolResultType, ErrorInfo, CancelledError, ToJson, FromJson
from .core.agent_state import StateEntry, StatePersistEntry, StateRuntimeEntry, JsonEntry, RuntimeEntry, AgentState
from .core.hooks import HookArgs, Hooks
from .core.command import Command, CommandRegistry
from .core.compact import CompactorAbstract, AutoCompactor
from .core.toolbox import ToolBox, ToolCallContext
from .core.agent_factory import AgentGetterProtocol, AgentGetterParam
from .core.toolcall import tool_attr
from .core.config import AgentConfig, xun_version, load_env
from .core.extension import ExtensionContext, extension_attr, extension_data_dir
from .core.workspace import Workspace
from .core.slot_semaphore import SlotSemaphore
from .core.agent import Agent

if TYPE_CHECKING:  # resolved via __getattr__
    from .displays import Display, WebDisplay, WebDisplayService
    from .entrypoint import (setup_agent, interactive_session, web_session,
                             main, main_serve, main_container)

_LAZY = {
    "displays": ("Display", "WebDisplay", "WebDisplayService"),
    "entrypoint": ("setup_agent", "interactive_session", "web_session",
                   "main", "main_serve", "main_container"),
}


def __getattr__(name: str):
    for module, names in _LAZY.items():
        if name in names:
            from importlib import import_module
            value = getattr(import_module(f".{module}", __name__), name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Agent", "AgentConfig",
    "Workspace", "SlotSemaphore",
    "tool_attr", "ToolBox", "ToolCallContext", "ExtensionContext", "extension_attr",
    "extension_data_dir",
    "xun_version", "load_env",
    "AgentGetterProtocol", "AgentGetterParam", 
    "DisplayAbstract", "Display", "NullDisplay", "WebDisplay", "WebDisplayService",
    "Command", "CommandRegistry",
    "HookArgs", "Hooks",
    "StateEntry", "StatePersistEntry", "StateRuntimeEntry", "JsonEntry", "RuntimeEntry", "AgentState",
    "CompactorAbstract", "AutoCompactor",
    "setup_agent", "interactive_session", "web_session",
    "main", "main_serve", "main_container",
    "Result", "ToolResultType", "ErrorInfo", "CancelledError", "ToJson", "FromJson",
]