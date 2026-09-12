"""Authenticated FastAPI web display for interactive agents."""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, AsyncGenerator, Callable, Literal, Optional, TYPE_CHECKING, Union

from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, TypeAdapter

from ..display_abstract import AgentInfo, DisplayAbstract, DisplayEvent, UserMessageEvent
from ..types import CancelledError
from .web_file import build_file_router
from ..agent import Agent  # runtime import: needed only for the Agent.is_initialized guard

if TYPE_CHECKING:
    from ..agent import Agent


class ChatMessage(BaseModel):
    type: Literal["message"]
    agent_id: str
    content: str = ""
    images: list[UserMessageEvent.ImageDescriptor] = Field(default_factory=list, max_length=8)


class CommandMessage(BaseModel):
    type: Literal["command"]
    agent_id: str
    name: str
    arguments: Optional[str] = None


class ChoiceMessage(BaseModel):
    type: Literal["choice"]
    prompt_id: str
    value: str


class CancelMessage(BaseModel):
    type: Literal["cancel"]
    agent_id: str


WebMessage = Annotated[Union[ChatMessage, CommandMessage, ChoiceMessage, CancelMessage], Field(discriminator="type")]
WEB_MESSAGE_ADAPTER = TypeAdapter(WebMessage)


class _EventStore:
    def __init__(self, max_events: int) -> None:
        self._events: deque[DisplayEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()

    def append(self, event: DisplayEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list(self) -> list[DisplayEvent]:
        with self._lock:
            return list(self._events)


@dataclass
class _PendingPrompt:
    data: dict[str, Any]
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[str] = None


class _PendingPrompts:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prompts: dict[str, _PendingPrompt] = {}

    def set(self, agent_id: str, prompt: dict[str, Any]) -> _PendingPrompt:
        with self._lock:
            prompt_id = secrets.token_urlsafe(12)
            pending = _PendingPrompt({"id": prompt_id, "agent_id": agent_id, **prompt})
            self._prompts[prompt_id] = pending
            return pending

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [prompt.data.copy() for prompt in self._prompts.values()]

    def respond(self, prompt_id: str, value: str) -> bool:
        with self._lock:
            pending = self._prompts.pop(prompt_id, None)
            if pending is None:
                return False
            pending.response = value
            pending.event.set()
            return True

    def wait(self, pending: _PendingPrompt) -> str:
        pending.event.wait()
        assert pending.response is not None
        return pending.response


class WebDisplay(DisplayAbstract):
    """Build a web interface and bridge browser input to agents."""

    def __init__(
        self,
        expose_files: bool = False,
        max_events: int = 2000,
    ) -> None:
        super().__init__()
        self.expose_files = expose_files
        self._store = _EventStore(max_events)
        self._pending = _PendingPrompts()
        self._clients: set[WebSocket] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._executor_lock = threading.Lock()

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncGenerator[None, None]:
        self._attach(asyncio.get_running_loop())
        try:
            yield
        finally:
            self._detach()

    def _attach(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not None:
            raise RuntimeError("WebDisplay is already attached to a running app")
        self._loop = loop

    def _detach(self) -> None:
        self._loop = None
        with self._executor_lock:
            executors = list(self._executors.values())
            self._executors.clear()
        for executor in executors:
            executor.shutdown(wait=False, cancel_futures=True)

    async def _close_clients(self) -> None:
        clients = tuple(self._clients)
        self._clients.clear()
        for client in clients:
            try:
                await client.close(code=1001)
            except Exception:
                pass

    def build_app(self) -> FastAPI:
        app = FastAPI(title="Xun Web", docs_url=None, redoc_url=None, lifespan=self._lifespan)
        app.include_router(self.build_routes())
        return app

    def bind(self, agent: "Agent[Agent.T.Uninit]") -> None:
        super().bind(agent)
        # running-state broadcasts come from the agent's run hooks, so no
        # tracking set is needed here; /api/running reads agent.is_running
        def broadcast_closure(running: bool) -> None:
            self._broadcast({"type": "execution_state", "agent_id": agent.identifier, "running": running})

        agent.hooks.run_start.add(lambda _args: broadcast_closure(True))
        agent.hooks.run_end.add(lambda _args: broadcast_closure(False))

    def on_event(self, event: DisplayEvent) -> None:
        payload = event.to_json()
        self._store.append(event)
        self._broadcast(payload)

    def get_choice(self, request: DisplayAbstract.ChoiceRequest) -> str:
        pending = self._pending.set(
            request.agent_info.identifier,
            request.model_dump(mode="json", exclude={"agent_info"}),
        )
        self._broadcast({"type": "pending_prompt", "data": pending.data})
        return self._pending.wait(pending)

    def _agent(self, identifier: str) -> "Agent[Agent.T.Any]":
        agent = self.agents.get(identifier)
        if agent is None:
            raise HTTPException(404, "Agent not found")
        return agent

    def _supports_vision(self, agent: "Agent[Agent.T.Init]") -> bool:
        return "vision" in agent.config.model.capabilities

    def _enqueue(self, agent_id: str, function: Any, *args: Any) -> None:
        with self._executor_lock:
            executor = self._executors.get(agent_id)
            if executor is None:
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"xun-web-{agent_id}")
                self._executors[agent_id] = executor
        executor.submit(function, *args)

    def _submit(self, message: WebMessage) -> None:
        if isinstance(message, ChatMessage):
            agent = self._agent(message.agent_id)
            content = message.content.strip()
            if not content and not message.images:
                return
            if not Agent.is_initialized(agent):
                agent.error("Agent is not initialized")
                return
            if message.images and not self._supports_vision(agent):
                agent.error("The configured model does not support image input")
                return
            images = [image.value for image in message.images]
            self._enqueue(message.agent_id, self._execute_message, agent, content, images)
        elif isinstance(message, CommandMessage):
            agent = self._agent(message.agent_id)
            name = message.name.strip().lstrip("/")
            if name:
                if not Agent.is_initialized(agent):
                    agent.error("Agent is not initialized")
                    return
                self._enqueue(message.agent_id, self._execute_command, agent, name, message.arguments)
        elif isinstance(message, CancelMessage):
            # cancel() is idle-safe on its own; no need to consult the tracking set
            self._agent(message.agent_id).cancel()
        else:
            if self._pending.respond(message.prompt_id, message.value):
                self._broadcast({"type": "prompt_resolved", "prompt_id": message.prompt_id})

    def _track(self, agent: "Agent[Agent.T.Init]", run: Callable[[], object]) -> None:
        # the CM keeps _running true across the whole tracked window (including the
        # retry/instruct gaps between entry-point CMs), so cancel() is effective
        # whenever the UI shows running; execution_state broadcasts are emitted by
        # the run_start hook registered in bind()
        try:
            with agent.cancellable_execution():
                run()
        except CancelledError:
            pass

    def _execute_message(self, agent: "Agent[Agent.T.Init]", content: str, images: list[str]) -> None:
        try:
            self._track(agent, lambda: agent.instruct(content, images=images or None).execute())
        except Exception as exc:
            agent.error(f"Error executing instruction: {exc}")

    def _execute_command(self, agent: "Agent[Agent.T.Init]", name: str, arguments: Optional[str]) -> None:
        def run() -> None:
            agent.execute_command(name, arguments)
            if name == "retry":
                agent.execute()

        self._track(agent, run)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            return

        async def send() -> None:
            stale: list[WebSocket] = []
            for client in tuple(self._clients):
                try:
                    await client.send_json(payload)
                except Exception:
                    stale.append(client)
            self._clients.difference_update(stale)

        asyncio.run_coroutine_threadsafe(send(), loop)

    def build_routes(self) -> APIRouter:
        router = APIRouter()

        @router.websocket("/ws")
        async def websocket(websocket: WebSocket) -> None:
            await websocket.accept()
            self._clients.add(websocket)
            try:
                while True:
                    self._submit(WEB_MESSAGE_ADAPTER.validate_python(await websocket.receive_json()))
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        @router.get("/api/events")
        async def events() -> list[DisplayEvent]:
            return self._store.list()

        @router.get("/api/prompts")
        async def pending_prompts() -> list[dict[str, Any]]:
            return self._pending.list()

        @router.post("/api/prompts/{prompt_id}")
        async def respond_to_prompt(prompt_id: str, response: ChoiceMessage) -> dict[str, bool]:
            if response.prompt_id != prompt_id or not self._pending.respond(prompt_id, response.value):
                raise HTTPException(409, "Prompt is no longer pending")
            self._broadcast({"type": "prompt_resolved", "prompt_id": prompt_id})
            return {"resolved": True}

        @router.get("/api/agents")
        async def agents() -> list[AgentInfo]:
            return [AgentInfo.from_agent(agent) for agent in self.agents.values()]

        @router.get("/api/running")
        async def running() -> list[str]:
            return sorted(agent.identifier for agent in self.agents.values() if agent.is_running)

        @router.get("/api/config")
        async def config() -> dict[str, Any]:
            return {"expose_files": self.expose_files}

        @router.get("/api/commands/{agent_id}")
        async def commands(agent_id: str) -> list[dict[str, str]]:
            agent = self._agent(agent_id)
            values = [{"name": "help", "description": "Show available commands."}]
            values.extend(
                {"name": command.name, "description": command.description}
                for command in agent.command.commands.values()
            )
            return values

        @router.get("/api/capabilities/{agent_id}")
        async def capabilities(agent_id: str) -> dict[str, Any]:
            model = self._agent(agent_id).config.model
            return {"model": model.name, "capabilities": sorted(model.capabilities)}

        if self.expose_files:
            router.include_router(build_file_router(self._agent))

        return router

