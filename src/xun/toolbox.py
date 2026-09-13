from __future__ import annotations
from typing import TYPE_CHECKING, Literal
if TYPE_CHECKING:
    from .agent import Agent, _Uninit
from typing import Callable
import fnmatch
from .tools import *
from .prompt import get_subagent_prompt
from .types import ModelCapabilityType, ToolResultType
from .error_catch import is_except_safe_wrapper, except_safe
from .toolcall import Function, ToolCallContext
from .config import get_internal_env_bool
import rich

class ToolBox:

    STANDARD_TOOL_SET_OPTIONS = Literal["system", "fs", "patch", "cmd", "search", "browser", "diagnostic", "extra"]
    STANDARD_TOOL_FACTORIES: dict[STANDARD_TOOL_SET_OPTIONS, Callable[[], list[Callable]]] = {
        "system": expose_system_tools,
        "fs": expose_fs_tools,
        "patch": expose_patch_tools,
        "cmd": expose_cmd_tools,
        "search": expose_search_tools,
        "browser": expose_browser_tools,
        "diagnostic": expose_diagnostic_tools,
        "extra": expose_extra_tools,
    }
    SUBAGENT_DEPTH_FLAG = "__subagent_depth"
    SUBAGENT_MAX_DEPTH = 3

    def __init__(self):
        self._tools: dict[str, Function] = {}
        self._disabled_tools: set[str] = set()

    def clone(self) -> "ToolBox":
        new_box = ToolBox()
        new_box._tools = self._tools.copy()
        new_box._disabled_tools = self._disabled_tools.copy()
        return new_box

    def with_defaults(self, *tool_set: STANDARD_TOOL_SET_OPTIONS) -> "ToolBox":
        """
        Register standard tools provided by the system.
        Call this method to quickly set up a toolbox with a wide range of capabilities for your agent.
        For toolset names, see ToolBox.STANDARD_TOOL_FACTORIES
        """
        if not tool_set:
            tool_set = tuple(self.STANDARD_TOOL_FACTORIES.keys())
        for tool_name in tool_set:
            assert tool_name in self.STANDARD_TOOL_FACTORIES, \
                f"Unknown standard tool set: {tool_name}. " \
                f"Available sets: {list(self.STANDARD_TOOL_FACTORIES.keys())}"
            factory = self.STANDARD_TOOL_FACTORIES[tool_name]
            self.register(*factory())
        return self

    def register(self, *funcs: Callable):
        for f in funcs:
            fn = f if is_except_safe_wrapper(f) else except_safe(f)
            wrapped = Function.from_function(fn)
            old = self._tools.get(wrapped.name)
            if old is None:
                self._tools[wrapped.name] = wrapped
                continue
            if wrapped.override:
                self._tools[wrapped.name] = wrapped
                msg = f"Overriding tool '{wrapped.name}'"
            elif old.override:
                msg = f"Kept existing override for '{wrapped.name}', skipped incoming"
            else:
                raise ValueError(f"Conflict tool name: {wrapped.name}")
            if get_internal_env_bool('INFO_TOOL_OVERRIDE'):
                rich.print(f"[blue]Info: {msg}[/blue]")
        return self

    def tool[F: Callable](self, func: F) -> F:
        """Decorator to register a function as a tool."""
        self.register(func)
        return func

    def with_subagent_provider(self, agent_getter: Callable[[ToolCallContext], "Agent[Agent.T.Uninit]"] | None = None):
        """
        Allow the agent to spawn sub-agents (worker) to execute tasks.
        The sub-agents can be customized by providing an agent_getter function.
        """
        if agent_getter is None:
            def _agent_getter(ctx: ToolCallContext) -> "Agent[_Uninit]":
                from .agent import Agent    # avoid circular import
                agent = Agent.inherit(ctx.agent).system(get_subagent_prompt())
                agent.state[self.SUBAGENT_DEPTH_FLAG] = ctx.agent.state.get(self.SUBAGENT_DEPTH_FLAG, 0)
                if agent.state[self.SUBAGENT_DEPTH_FLAG] >= self.SUBAGENT_MAX_DEPTH:
                    agent.toolbox.disable_subagent()
                agent.state[self.SUBAGENT_DEPTH_FLAG] = agent.state.get(self.SUBAGENT_DEPTH_FLAG, 0) + 1
                return agent
            agent_getter = _agent_getter
        self.register(agent_run_factory(agent_getter))
        self.register(agent_run_parallel_factory(agent_getter))
        return self

    def disable_subagent(self):
        """ Useful tool for quickly disabling the sub-agent spawning capabilities of the agent.  """
        self.disable("agent_run", "agent_run_parallel")
        return self

    def enable_subagent(self):
        """ Useful tool for quickly enabling the sub-agent spawning capabilities of the agent.  """
        self.enable("agent_run", "agent_run_parallel")
        return self

    def _resolve_tool_names(self, *patterns: str) -> set[str]:
        """ Resolve names/glob-patterns to a set of actual tool names.
        Plain names are returned as-is.  """
        WILDCARD_CHARS = frozenset("*?[]")

        # fast path: no wildcards at all
        if not any(WILDCARD_CHARS & set(p) for p in patterns):
            return set(patterns)

        all_names = set(self._tools) | self._disabled_tools
        resolved: set[str] = set()
        for pattern in patterns:
            if WILDCARD_CHARS & set(pattern):
                for name in all_names:
                    if fnmatch.fnmatch(name, pattern):
                        resolved.add(name)
            else:
                resolved.add(pattern)
        return resolved

    def disable(self, *tool_names: str) -> "ToolBox":
        """Disable tools by exact name or glob pattern. wildcard patterns are supported."""
        self._disabled_tools.update(self._resolve_tool_names(*tool_names))
        return self

    def enable(self, *tool_names: str) -> "ToolBox":
        """Enable previously-disabled tools by exact name or glob pattern. wildcard patterns are supported."""
        self._disabled_tools.difference_update(self._resolve_tool_names(*tool_names))
        return self

    def list_tools(self, model_capabilities: set[ModelCapabilityType] | None = None) -> list[Function]:
        return [
            tool
            for name, tool in self._tools.items()
            if name not in self._disabled_tools and 
            (model_capabilities is None or tool.required_capabilities.issubset(model_capabilities))
        ]

    def call_tool(
        self,
        agent: "Agent[Agent.T.Init]",
        tool_name: str,
        arguments: dict,
        context
    ) -> ToolResultType:
        if tool_name in self._disabled_tools:
            raise ValueError(f"Tool '{tool_name}' is disabled.")
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ValueError(f"Tool '{tool_name}' is not registered.")
        return tool.call(
            arguments,
            ToolCallContext(agent=agent, tool_name=tool_name, v=context)
        )

    def list_tools_json(self, model_capabilities: set[ModelCapabilityType] | None = None):
        return [ tool.tool_schema for tool in self.list_tools(model_capabilities) ]