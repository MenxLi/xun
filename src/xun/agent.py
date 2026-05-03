from openai import OpenAI
from typing import Any
import json
import json_repair
from pathlib import Path
from tempfile import TemporaryDirectory
import uuid

from .display_abstract import *
from .display import Display
from .conversation import Conversation
from .config import app_config
from .prompt import get_condense_prompt
from .toolbox import ToolBox, extract_tool_calls
from .context import global_context, ToolCallContext, tool_call_context, ExecutionContext, execution_context

class Agent:
    def __init__(
        self, 
        name: str = "agent", 
        toolbox: ToolBox | None = None,
        openai_client: OpenAI | None = None, 
        persistent_store: Path | None = None,
        display: DisplayAbstract | None = None,
        ):
        self.name = name
        self.app_config = app_config()

        if openai_client is None:
            openai_client = OpenAI(
                base_url = self.app_config.provider.openai_base_url,
                api_key = self.app_config.provider.openai_api_key,
            )
        
        if toolbox is None:
            toolbox = ToolBox()

        self.toolbox = toolbox
        self.openai_client = openai_client

        self.conversation = Conversation()

        if persistent_store:
            if persistent_store.exists():
                assert persistent_store.is_dir(), f"Persistent store path {persistent_store} must be a directory."
                self.load(persistent_store)
            self.display.emit(InfoEvent(message=f"Using persistent store from {persistent_store}"))
        self.persistent_store = persistent_store

        if display:
            self.display = display
        else:
            self.display = Display()

    def dump(self, store_dir: Path):
        if not store_dir.exists():
            store_dir.mkdir(exist_ok=True)
        conv_file = store_dir / f"conversation.json"
        self.conversation.dump(conv_file)
    
    def load(self, store_dir: Path):
        conv_file = store_dir / f"conversation.json"
        if conv_file.exists():
            self.conversation.load(conv_file)
        else:
            self.display.emit(ErrorEvent(message=f"No conversation history found in {conv_file}. Starting with an empty conversation."))
    
    def _dump(self):
        if self.persistent_store:
            self.dump(self.persistent_store)
    
    def _execute(self, max_iterations: int = 64) -> str:
        if max_iterations <= 0:
            self.display.emit(ErrorEvent(message="Maximum tool call iterations exceeded."))
            return "[Error: Maximum tool call iterations exceeded.]"

        model_call_id = str(uuid.uuid4())
        self.display.emit(ModelWorkingEvent(model_call_id=model_call_id, remaining_iterations=max_iterations))
        n_max_retries = 5
        while True:
            try:
                resp = self.openai_client.chat.completions.create(
                    model=self.app_config.provider.openai_model,
                    tools = self.toolbox.list_tools_json(),     # type: ignore
                    tool_choice="auto",
                    messages = self.conversation.messages, 
                    timeout = 600,
                )
                break

            except KeyboardInterrupt:
                # remove last message if from user, to allow retry
                if self.conversation.messages and self.conversation.messages[-1]["role"] == "user":
                    self.conversation.messages.pop()
                self.display.emit(ErrorEvent(message="Execution interrupted by user."))
                return "[Error: Execution interrupted by user.]"

            except Exception as e:
                self.display.emit(ErrorEvent(message=f"Error during chat completion: {e}"))
                if n_max_retries > 0 and self.display.get_confirm("Retry?", default=True):
                    n_max_retries -= 1
                    continue
                else:
                    raise e

        choice = extract_tool_calls(resp.choices[0])

        if choice.message.content:
            self.display.emit(ModelMessageEvent(model_call_id=model_call_id, content=choice.message.content))
        self.conversation.add_agent_message(choice.message)
        self._dump()

        __tool_called = False
        if choice.message.tool_calls:

            for tool_call in choice.message.tool_calls:
                if tool_call.type != "function":
                    self.display.emit(ErrorEvent(message=f"Unsupported tool call type: {tool_call.type}"))
                    continue

                tool_id = tool_call.id
                tool_name = tool_call.function.name
                arguments = tool_call.function.arguments

                try:
                    tool_call_context.set(ToolCallContext(
                        agent=self,
                        tool_name=tool_name,
                        ))
                    arguments_json: Any = json_repair.loads(arguments)
                    self.display.emit(ToolCallEvent(tool_call_id=tool_id, tool_name=tool_name, args=arguments_json))
                    res = self.toolbox.call_tool_json(tool_name, arguments_json)
                    self.display.emit(ToolResultEvent(tool_call_id=tool_id, result=res))
                    tool_result = json.dumps(res if isinstance(res, dict) else res)
                except Exception as e:
                    self.display.emit(ErrorEvent(message=f"Tool {tool_name} failed: {e}"))
                    tool_result = json.dumps({
                        "error": str(e),
                    })
                finally:
                    tool_call_context.set(None)

                self.conversation.add_tool_call(tool_id, tool_result)
                __tool_called = True
        
        if __tool_called:
            self._dump()
            return self._execute(max_iterations=max_iterations - 1)
        
        return choice.message.content or "[No content]"

    def execute(self, max_iterations: int = 64) -> str:
        with TemporaryDirectory(prefix=f"{self.name}_", delete=True) as temp_dir_path:
            temp_dir = Path(temp_dir_path)
            global_context.lock().tempdirs.add(temp_dir)
            execution_context.set(ExecutionContext(
                agent=self, 
                tempdir=temp_dir,
                ))
            try:
                return self._execute(max_iterations=max_iterations)
            finally:
                execution_context.set(None)
                global_context.lock().tempdirs.remove(temp_dir)
    
    def system(self, content: str):
        self.conversation.set_system_message_content(content)
        return self
    
    def instruct(self, instruction: str):
        self.conversation.add_user_instruct(instruction)
        return self
    
    def condense_conversation(self):
        _condense_conversation(self)

def _condense_conversation(agent: Agent):
    """
    Condense the conversation history of the agent by keeping only the last user message and the assistant messages after that. 
    """
    agent.display.emit(InfoEvent(message="Condensing conversation history..."))

    keep_messages = agent.conversation.pop_from_last_user_message()
    condense_messages = agent.conversation.messages
    
    if not condense_messages:
        # revert
        agent.conversation.messages = condense_messages + keep_messages
        return
    
    client = agent.openai_client
    condense_messages_json = json.dumps(condense_messages, indent=4)
    resp = client.chat.completions.create(
        model=agent.app_config.provider.openai_model,
        messages = [
            {
                "role": "user",
                "content": get_condense_prompt(condense_messages_json),
            },
        ],
        timeout = 300,
    )
    summary = resp.choices[0].message.content
    if summary is None:
        agent.display.emit(ErrorEvent(message="Failed to condense conversation history: no summary generated."))
        return
    agent.display.emit(InfoEvent(message=f"Conversation history condensed. Summary:\n{summary}"))

    sys_msg = f"You are an assistant having a conversation with a user. Here is the summary of the conversation history so far:\n{summary}"
    agent.conversation.set_system_message_content(sys_msg)
    agent.conversation.messages += keep_messages
    return