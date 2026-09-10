from __future__ import annotations
from typing import Any, Sequence, Optional, Generic, TypeGuard, cast, overload
from dataclasses import dataclass, field
from pathlib import Path
import json
import uuid
import weakref

from openai import OpenAI
from pydantic import BaseModel
from PIL.Image import Image
from threading import Semaphore

from .types import TypeVar, CancelledError
from .display_abstract import *
from .running_state import AgentRunningStateMixin, LabeledEvent
from .displays.display import Display
from .conversation import Conversation
from .config import AgentConfig, load_config
from .prompt import get_condense_prompt
from .error_catch import except_safe
from .toolbox import ToolBox
from .workspace import Workspace
from .command import CommandRegistry
from .hooks import Hooks, HookArgs
from .loop import execution_loop, ExecutionLoopParams

DEFAULT_MAX_ITERATIONS = 128
DEFAULT_API_CALL_LIMIT = 3

_AUTO_CONFIRM_WARNED = False

def _warn_auto_confirm_once(agent: "Agent") -> None:
    """Warn once per process if the agent runs with auto-confirm enabled."""
    global _AUTO_CONFIRM_WARNED
    if _AUTO_CONFIRM_WARNED or not agent.config.auto_confirm:
        return
    _AUTO_CONFIRM_WARNED = True
    import rich, rich.panel
    rich.print(
        rich.panel.Panel(
            "[bold yellow]Auto-confirm is enabled.[/bold yellow]\nPlease be cautious as the agent may execute actions without confirmation, including potentially harmful commands if misused.\nIt's recommended to keep this setting disabled unless you have a specific use case that requires it.",
            title="[bold red]Warning[/bold red]", border_style="red"
            ),
        )

class _AgentState: v=0
class _Uninit(_AgentState): v=1
class _Init(_AgentState): v=2
class _Final(_AgentState): v=3

# covariant: an Agent[T.Init] is usable anywhere an Agent[T.Any] is expected, 
# but not vice versa. default=_Uninit: a bare `Agent` denotes a freshly constructed agent,
StateT = TypeVar("StateT", bound=_AgentState, covariant=True, default=_Uninit)
_ST = TypeVar("_ST", bound=_AgentState, covariant=True)

class T:
    """
    Namespace for type-level lifecycle states for annotations: 
    `Agent[T.Uninit]` / `Agent[T.Init]` / ...
    """
    Uninit = _Uninit
    Init = _Init
    Final = _Final
    Alive = _Uninit | _Init
    Any = _AgentState

@dataclass
class Agent(AgentDisplayMixin, AgentRunningStateMixin, Generic[StateT]):

    # class-level shorthand so callers can use `Agent[Agent.T.Init]`.
    # Must be a plain class attribute (NOT a PEP 695 `type` alias): a `type T = T`
    # turns Agent.T into a TypeAliasType whose attributes Pylance won't resolve.
    T = T

    name: str = field(default_factory=lambda: f"agent-{str(uuid.uuid4())[:8]}")
    identifier: str = field(default_factory=lambda: str(uuid.uuid4()))
    display: DisplayAbstract = field(default_factory=Display)
    conversation: Conversation = field(default_factory=Conversation)
    toolbox: ToolBox = field(default_factory=ToolBox)
    command: CommandRegistry = field(default_factory=CommandRegistry)
    workspace: Workspace = field(default_factory=Workspace)
    persistent_store: Optional[Path] = None
    cancel_event: LabeledEvent = field(default_factory=lambda: LabeledEvent(label=""))

    # below auto inherit
    config: AgentConfig = field(default_factory=lambda: load_config().clone())
    api_call_semaphore: Semaphore = field(default_factory=lambda: Semaphore(DEFAULT_API_CALL_LIMIT))

    # below does not inherit
    state: dict[str, Any] = field(default_factory=dict)
    hooks: Hooks = field(default_factory=Hooks)

    _openai_client: OpenAI = field(init=False, repr=False)
    _lifecycle: StateT = field(init=False, repr=False, default_factory=lambda: cast(StateT, _Uninit()))
    _running: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        if self.cancel_event.label == "":
            self.cancel_event.label = self.identifier

        # note: the callback must not hold a strong reference to the agent
        # (weakref.finalize keeps its arguments alive), hence the weakref idiom
        agent_ref = weakref.ref(self)
        weakref.finalize(self, Agent._finalize, agent_ref)
    
    def _cast_self(self: Agent[T.Any], s: type[_ST]) -> Agent[_ST]:
        """Cast self to a different lifecycle state. Use with care."""
        self._lifecycle = s()
        return cast(Agent[_ST], self)
    
    @property
    def openai_client(self) -> OpenAI:
        if not hasattr(self, "_openai_client"):
            self._openai_client = OpenAI(
                base_url = self.config.provider.openai_base_url,
                api_key = self.config.provider.openai_api_key,
            )
        return self._openai_client

    def initialize(self: Agent[T.Uninit]) -> Agent[T.Init]:
        """
        Initialize the agent, cannot be called on a finalized agent. 
        Returns a Agent[T.Init]

        Must rebind (shadow) on call for proper type-level state, e.g.
        ```python
        agent = Agent()
        agent = agent.initialize()  # rebind
        ```
        """
        if self._lifecycle.v == T.Final.v:
            raise RuntimeError(f"Agent '{self.name}' has been finalized; it cannot be re-initialized.")
        if self._lifecycle.v == T.Init.v:
            return cast(Agent[T.Init], self)

        if self.config.model.name == "":
            self.config.model._assign_primary_model(self.openai_client)

        _warn_auto_confirm_once(self)

        self.display.bind(self)
        self.display_event(AgentBindEvent())
        if self.persistent_store:
            if self.persistent_store.exists():
                assert self.persistent_store.is_dir(), f"Persistent store path {self.persistent_store} must be a directory."
                self.load(self.persistent_store)
            self.info(f"Using persistent store from {self.persistent_store}")

        self.workspace.prepare()

        initialized_self = self._cast_self(_Init)
        self.hooks.after_initialize.invoke(HookArgs.AfterInitializeArgs(agent=initialized_self))
        return initialized_self

    @staticmethod
    def is_initialized(agent: Agent[T.Any]) -> TypeGuard[Agent[T.Init]]:
        """Type guard: True when the agent is initialized (and not finalized).

        Stands as a staticmethod (call as Agent.is_initialized(agent)) because Pylance only
        accepts user-defined TypeGuards with at least one explicit parameter.
        """
        return agent._lifecycle.v == T.Init.v

    @staticmethod
    def is_finalized(agent: Agent[T.Any]) -> TypeGuard[Agent[T.Final]]:
        """Type guard: True when the agent has been finalized and must not be used further."""
        return agent._lifecycle.v == T.Final.v

    @staticmethod
    def inherit(
        parent_agent: Agent[T.Any], 
        share_workspace: bool = True,
        share_display: bool = True,
        share_cancel_event: bool = True,
        copy_toolbox: bool = True,
        copy_command: bool = True,
        copy_conversation: bool = False,
        persistent_store: Optional[Path] = None, 
        ) -> "Agent[T.Uninit]":
        """
        Create a new agent that inherits the configuration and state from the parent agent.
        The default behavior is mostly that of the default sub-agent getter
        """
        new_agent = Agent(
            identifier=(new_id := str(uuid.uuid4())),
            name=f"{parent_agent.name}-child-{new_id[:8]}",
            persistent_store=persistent_store,
            # auto inherit
            config = parent_agent.config.clone(),
            api_call_semaphore=parent_agent.api_call_semaphore,
            display=parent_agent.display if share_display else Display(),
        )
        if share_workspace:
            # share the whole on-disk footprint: same workdir and same (lazy) tempdir
            new_agent.workspace = parent_agent.workspace
        if copy_toolbox:    
            new_agent.toolbox = parent_agent.toolbox.clone()
        if copy_command:
            new_agent.command = parent_agent.command.clone()
        if copy_conversation:
            new_agent.conversation.messages = parent_agent.conversation.messages.copy()
        if share_cancel_event:
            new_agent.cancel_event = parent_agent.cancel_event
        return new_agent

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
            self.error(f"No conversation history found in {conv_file}. Starting with an empty conversation.")
    
    @overload
    @except_safe
    def execute[T: BaseModel](
        self: Agent[Agent.T.Init], schema: type[T],  # explicit Agent.T: method typevar T shadows the module alias
        max_iterations: int = DEFAULT_MAX_ITERATIONS, 
        context: Any = None
    ) -> T: ...
    @overload
    @except_safe
    def execute(
        self: Agent[T.Init], schema: None = None, 
        max_iterations: int = DEFAULT_MAX_ITERATIONS, 
        context: Any = None
    ) -> str: ...
    @except_safe
    def execute(
        self: Agent[T.Init], schema: Optional[type[BaseModel]] = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        context: Any = None
        ):
        if not Agent.is_initialized(self):
            raise RuntimeError(f"Agent '{self.name}' is not initialized. Call agent.initialize() or use 'with agent:'.")

        try:
            with self.cancellable_execution():
                return execution_loop(ExecutionLoopParams(
                    agent=self, schema=schema, max_iterations=max_iterations, context_value=context
                ))
        except CancelledError:
            self.error("Execution cancelled by user.")
            raise

    def system[AliveT: Agent[T.Alive]](self: AliveT, content: str) -> AliveT:
        self.conversation.set_system_message_content(content)
        return self

    def instruct[AliveT: Agent[T.Alive]](
        self: AliveT,
        instruction: str,
        images: Sequence[str | Image] | None = None,
        _emit_event: bool = True,
    ) -> AliveT:
        self.conversation.add_user_message(instruction, images=images)
        if _emit_event:
            self.display_event(UserMessageEvent.from_inputs(instruction, images=images))
        return self
    
    def execute_command(self: "Agent[T.Init]", command_name: str, arguments: Optional[str] = None):
        command = self.command.get(command_name)
        self.display_event(UserCommandEvent(name=command_name, arguments=arguments))
        if command is None:
            self.error(f"Unknown command: {command_name}")
            return
        # commands may run the model (continue/retry/compact), so they are running work too
        with self.cancellable_execution():
            command.invoke(self, arguments)
    
    def condense_conversation(self: "Agent[T.Init]"):
        _condense_conversation(self)
    
    def __enter__(self: "Agent[T.Uninit]") -> "Agent[T.Init]":
        # any state: entering an already-initialized agent (e.g. a configured one returned
        # by a sub-agent getter) is fine because initialize() is runtime-idempotent.
        return self.initialize()
    
    def __exit__(self: Agent[T.Alive], exc_type, exc_value, traceback):
        if Agent.is_initialized(self):
            self.finalize()
    
    def finalize(self: Agent[T.Any]) -> Agent[T.Final]:
        """
        Finalize the agent, 
        while it accepts any lifecycle state, 
        it is a no-op on an agent that is already finalized or was never initialized.

        Must rebind on call for proper type-level state, like initialize().
        """
        if Agent.is_finalized(self):
            return self
        if not Agent.is_initialized(self):
            return self._cast_self(_Final)
        self.hooks.before_finalize.invoke(HookArgs.BeforeFinalizeArgs(agent=self))
        self.display.unbind(self)
        self.display_event(AgentUnbindEvent())
        return self._cast_self(_Final)

    @staticmethod
    def _finalize(agent_ref: "weakref.ref"):
        # weakref callback: the agent's state is unknown at GC time
        if (agent := agent_ref()) is not None and Agent.is_initialized(agent):
            agent.finalize()

def _condense_conversation(agent: "Agent[Agent.T.Init]"):
    """
    Condense the conversation history of the agent by keeping only the last user message and the assistant messages after that. 
    """
    agent.info("Condensing conversation history...")

    keep_messages = agent.conversation.pop_from_last_user_message()
    condense_messages = agent.conversation.messages
    
    if not condense_messages:
        # revert
        agent.conversation.messages = condense_messages + keep_messages
        return
    
    client = agent.openai_client
    condense_messages_json = json.dumps(condense_messages, indent=4)
    with agent.api_call_semaphore:
        resp = client.chat.completions.create(
            model=agent.config.model.name,
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
        agent.error("Failed to condense conversation history: no summary generated.")
        return
    agent.info(f"Conversation history condensed. Summary:\n{summary}")

    sys_msg = f"You are an assistant having a conversation with a user. Here is the summary of the conversation history so far:\n{summary}"
    agent.conversation.set_system_message_content(sys_msg)
    agent.conversation.messages += keep_messages
    return