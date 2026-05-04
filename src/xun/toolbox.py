from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .agent import Agent
from typing import Callable, TypeVar
import weakref, threading
import mcp
from openai.types import chat
from fastmcp import Client, FastMCP
import asyncio
from .tools import *
from .prompt import get_subagent_prompt
from ._toolcall_fix import extract_tool_calls_from_text

def tool_to_openai_format(tool: mcp.types.Tool):
    schema = tool.inputSchema
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": schema
        }
    }

F = TypeVar("F", bound=Callable)
class ToolBox:
    STANDARD_TOOL_FACTORIES: list[Callable[[], list[Callable]]] = [
        expose_system_tools,
        expose_fs_tools, 
        expose_cmd_tools,
        expose_search_tools,
        expose_browser_tools,
    ]
    def __init__(self):
        self._mcp: FastMCP = FastMCP()
        self._client = Client(self._mcp)
        self._disabled_tools: set[str] = set()

        def loop_start(loop: asyncio.AbstractEventLoop):
            asyncio.set_event_loop(loop)
            loop.run_forever()
        loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(target=loop_start, args=(loop,), daemon=True)
        _loop_thread.start()
        self._loop = loop
        def loop_stop(loop: asyncio.AbstractEventLoop):
            loop.call_soon_threadsafe(loop.stop)
            _loop_thread.join()
        weakref.finalize(self, loop_stop, loop)
    
    def with_defaults(self):
        """
        Register all standard tools provided by the system. 
        Call this method to quickly set up a toolbox with a wide range of capabilities for your agent.
        """
        for tool_set_fn in self.STANDARD_TOOL_FACTORIES:
            tool_set = tool_set_fn()
            self.register_many(tool_set)
        return self
    
    def register(self, f: F) -> F:
        return self._mcp.tool()(f)
    
    def register_many(self, funcs: list[Callable]) -> list[Callable]:
        return [ self.register(func) for func in funcs ]
    
    def with_subagent_provider(self, agent_getter: Callable[[], "Agent"] | None = None):
        """
        Allow the agent to spawn sub-agents (worker) to execute tasks. 
        The sub-agents can be customized by providing an agent_getter function.
        """
        if agent_getter is None:
            def _agent_getter():
                from .agent import Agent    # avoid circular import
                from .context import tool_call_context
                tool_context = tool_call_context.get()
                if tool_context is None:
                    raise RuntimeError("tool_call_context is not set, cannot create sub-agent")
                agent = Agent(
                    toolbox=ToolBox().with_defaults(),
                    display=tool_context.display,
                ).system(get_subagent_prompt())
                return agent
            agent_getter = _agent_getter
        self.register(agent_run_factory(agent_getter))
        self.register(agent_run_parallel_factory(agent_getter))
        return self
    
    def disable(self, tool_name: str):
        self._disabled_tools.add(tool_name)
    
    def list_tools(self):
        async def _list_tools():
            async with self._client:
                tools = await self._client.list_tools()
                return [ tool for tool in tools if tool.name not in self._disabled_tools ]
        return asyncio.run_coroutine_threadsafe(_list_tools(), self._loop).result()
    
    def call_tool(self, tool_name: str, arguments: dict):
        async def _call_tool():
            async with self._client:
                return await self._client.call_tool(
                    name=tool_name,
                    arguments=arguments,
                )
        # should capture context at submission time
        # see test/test_context_var.py
        return asyncio.run_coroutine_threadsafe(_call_tool(), self._loop).result()

    def list_tools_json(self):
        tools = self.list_tools()
        return [ tool_to_openai_format(tool) for tool in tools ]
    
    def call_tool_json(self, tool_name: str, arguments: dict):
        return self.call_tool(tool_name, arguments).structured_content
    

def extract_tool_calls(choice: chat.chat_completion.Choice) -> chat.chat_completion.Choice:
    if choice.message.tool_calls:
        return choice

    # https://github.com/vllm-project/vllm/issues/39056
    # https://github.com/vllm-project/vllm/issues/29192

    content = choice.message.content
    if content is None:
        return choice

    cleaned, tool_calls = extract_tool_calls_from_text(content)

    choice.message.content = cleaned
    # dict to list of ToolCall
    tool_calls_typed: list[chat.chat_completion_message_function_tool_call.ChatCompletionMessageFunctionToolCall] = []
    for tc in tool_calls:
        tool_calls_typed.append(
            chat.chat_completion_message_function_tool_call.ChatCompletionMessageFunctionToolCall(
                id=tc["id"],
                type="function",
                function=chat.chat_completion_message_function_tool_call.Function(
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                ),
            )
        )
    
    choice.message.tool_calls = tool_calls_typed    # type: ignore
    return choice

