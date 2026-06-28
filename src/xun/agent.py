from openai import OpenAI
from typing import Any, Sequence
import json, weakref
import json_repair
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import dataclass, field
import uuid
from PIL.Image import Image

from .error_catch import except_safe
from .display_abstract import *
from .display import Display
from .conversation import Conversation
from .config import app_config
from .prompt import get_condense_prompt
from .toolbox import ToolBox, extract_tool_calls
from .context import global_context_guard, ToolCallContext, tool_call_context, ExecutionContext, execution_context

class AgentTempDir:
    """
    An abstraction for temporary directory,
    if pth is given, no cleanup will be performed. 
    Otherwise, a temporary directory will be lazily created and automatically cleaned up when the instance is garbage collected.
    """
    def __init__(self, pth: Optional[Path] = None):
        self._dir = pth
        self._temp_dir: Optional[TemporaryDirectory] = None
        if self._dir is not None:
            assert self._dir.exists() and self._dir.is_dir(), f"Path {self._dir} does not exist or is not a directory."
        
        with global_context_guard as global_context:
            global_context.tempdirs.add(self)

        def maybe_cleanup_temp_dir():
            if self._temp_dir is not None:
                self._temp_dir.__exit__(None, None, None)
                self._temp_dir = None
            with global_context_guard as global_context:
                global_context.tempdirs.discard(self)
        weakref.finalize(self, maybe_cleanup_temp_dir)

    @property
    def path(self) -> Path:
        if self._dir is not None:
            return self._dir
        else:
            if self._temp_dir is None:
                self._temp_dir = TemporaryDirectory()
            return Path(self._temp_dir.name)
    
    @property
    def exist_path(self) -> Optional[Path]:
        if self._dir is not None:
            return self._dir
        else:
            return self._temp_dir and Path(self._temp_dir.name)

def _default_openai_client():
    config = app_config()
    return OpenAI(
        base_url = config.provider.openai_base_url,
        api_key = config.provider.openai_api_key,
    )

@dataclass()
class Agent:
    name: str = field(default_factory=lambda: f"agent-{str(uuid.uuid4())[:8]}")
    identifier: str = field(default_factory=lambda: str(uuid.uuid4()))
    display: DisplayAbstract = field(default_factory=Display)
    conversation: Conversation = field(default_factory=Conversation)
    toolbox: ToolBox = field(default_factory=ToolBox)
    openai_client: OpenAI = field(default_factory=_default_openai_client)
    tempdir: AgentTempDir = field(default_factory=AgentTempDir)
    persistent_store: Path | None = None

    def __post_init__(self):
        if self.persistent_store:
            if self.persistent_store.exists():
                assert self.persistent_store.is_dir(), f"Persistent store path {self.persistent_store} must be a directory."
                self.load(self.persistent_store)
            self.display.emit(InfoEvent(message=f"Using persistent store from {self.persistent_store}"))
    
    @property
    def app_config(self):
        return app_config()

    def dump(self, store_dir: Optional[Path] = None):
        if store_dir is None:
            if self.persistent_store is None:
                return
            store_dir = self.persistent_store
        if not store_dir.exists():
            store_dir.mkdir(exist_ok=True)

        conv_file = store_dir / f"conversation.json"
        self.conversation.dump(conv_file)
    
    def load(self, store_dir: Optional[Path] = None):
        if store_dir is None:
            if self.persistent_store is None:
                raise ValueError("Persistent store path is not set. Please provide a store_dir to load the conversation.")
            store_dir = self.persistent_store

        conv_file = store_dir / f"conversation.json"
        if conv_file.exists():
            self.conversation.load(conv_file)
        else:
            self.display.emit(ErrorEvent(message=f"No conversation history found in {conv_file}. Starting with an empty conversation."))
    
    def _execute(self, call_id: str) -> tuple[bool, str]:
        n_completion_max_retries = 3
        while True:
            try:
                params = {
                    "model": self.app_config.provider.openai_model,
                    "messages": self.conversation.messages,
                    "timeout": 600,
                }
                if (tools_json := self.toolbox.list_tools_json()):
                    params["tools"] = tools_json
                    params["tool_choice"] = "auto"

                resp = self.openai_client.chat.completions.create(**params)
                break

            except KeyboardInterrupt:
                # remove last message if from user, to allow retry
                self.conversation.pop_last_message_if_user()
                self.display.emit(ErrorEvent(message="Execution interrupted by user."))
                return False, "[Error: Execution interrupted by user.]"

            except Exception as e:
                self.display.emit(ErrorEvent(message=f"Error during chat completion: {e}"))
                if n_completion_max_retries > 0 and self.display.get_confirm("Retry?", default=True):
                    n_completion_max_retries -= 1
                    continue
                else:
                    raise e

        choice = extract_tool_calls(resp.choices[0])

        if choice.message.content:
            self.display.emit(ModelMessageEvent(model_call_id=call_id, content=choice.message.content))
        self.conversation.add_agent_message(choice.message)
        self.dump()

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
            self.dump()
        
        return __tool_called, choice.message.content or "[No content]"

    @except_safe
    def execute(self, max_iterations: int = 64) -> str:
        execution_context.set(ExecutionContext( agent=self, ))
        try:
            for iteration in range(max_iterations):
                model_call_id = str(uuid.uuid4())
                self.display.emit(ModelWorkingEvent(
                    model_call_id=model_call_id, 
                    remaining_iterations=max_iterations - iteration
                    ))
                should_continue, result = self._execute(model_call_id)
                if not should_continue:
                    return result

            self.display.emit(ErrorEvent(message="Maximum tool call iterations exceeded."))
            raise RuntimeError("Maximum tool call iterations exceeded.")

        finally:
            execution_context.set(None)
    
    def system(self, content: str):
        self.conversation.set_system_message_content(content)
        return self
    
    def instruct(self, instruction: str, images: Sequence[str | Image] | None = None):
        self.conversation.add_user_message(instruction, images=images)
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