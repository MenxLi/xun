"""Authenticated FastAPI web display for interactive agents."""

from __future__ import annotations

import asyncio
import hmac
import secrets
import socket
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, AsyncGenerator, Callable, Literal, Optional, TYPE_CHECKING, Union
from urllib.parse import quote, urlencode, urlsplit

import uvicorn
import jinja2
from fastapi import APIRouter, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, TypeAdapter
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from ..config import ASSET_DIR
from ..display_abstract import AgentInfo, DisplayAbstract, DisplayEvent, UserMessageEvent
from ..types import CancelledError
from .file_api import build_file_router
from ..agent import Agent  # runtime import: needed only for the Agent.is_initialized guard

if TYPE_CHECKING:
    from ..agent import Agent


DEFAULT_WEB_ASSETS = ASSET_DIR / "web"
LOGIN_TEMPLATE = jinja2.Environment(autoescape=True).from_string(
    (ASSET_DIR / "login.template.html").read_text(encoding="utf-8")
)
_COOKIE_NAME = "xun_web_token"


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


class _TokenAuthMiddleware:
    def __init__(self, app: Any, token: str, mounts: dict[str, WebDisplay]) -> None:
        self.app = app
        self.token = token
        self.mounts = mounts

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        root_path = scope.get("root_path", "").rstrip("/")
        bearer = connection.headers.get("authorization", "")
        header_token = bearer[7:] if bearer.lower().startswith("bearer ") else ""
        cookie_token = connection.cookies.get(_COOKIE_NAME, "")
        if _tokens_match(header_token, self.token) or _tokens_match(cookie_token, self.token):
            await self.app(scope, receive, send)
            return

        request_path = connection.url.path
        if root_path and request_path.startswith(root_path):
            request_path = request_path[len(root_path):] or "/"
        mount_path = request_path.rstrip("/")
        if request_path == "/login":
            await self.app(scope, receive, send)
            return
        query_token = connection.query_params.get("token")
        if scope["type"] == "http" and mount_path in self.mounts and _tokens_match(query_token or "", self.token):
            response = RedirectResponse("./", status_code=303)
            response.set_cookie(
                _COOKIE_NAME,
                self.token,
                httponly=True,
                samesite="strict",
                secure=connection.url.scheme == "https",
                path=f"{root_path}/" if root_path else "/",
            )
            await response(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        elif self._is_display_page_request(scope, connection, request_path):
            login_path = f"{root_path}/login" if root_path else "/login"
            target = request_path
            if connection.url.query:
                target = f"{target}?{connection.url.query}"
            response = RedirectResponse(f"{login_path}?{urlencode({'next': target})}", status_code=303)
            await response(scope, receive, send)
        else:
            response = JSONResponse({"detail": "Not authenticated"}, status_code=401)
            await response(scope, receive, send)

    def _is_display_page_request(
        self,
        scope: dict[str, Any],
        connection: HTTPConnection,
        request_path: str,
    ) -> bool:
        if scope.get("method") not in {"GET", "HEAD"}:
            return False
        for mount_path in self.mounts:
            if mount_path and request_path != mount_path and not request_path.startswith(f"{mount_path}/"):
                continue
            relative_path = request_path[len(mount_path):] if mount_path else request_path
            if relative_path in {"", "/"}:
                return True
            if relative_path == "/api" or relative_path.startswith("/api/") or relative_path == "/ws":
                return False
            return "text/html" in connection.headers.get("accept", "")
        return False


def _tokens_match(value: str, expected: str) -> bool:
    return bool(value) and hmac.compare_digest(value, expected)


def _normalize_base_path(value: str) -> str:
    stripped = value.strip().strip("/")
    if not stripped:
        return ""
    if any(part in {".", ".."} for part in stripped.split("/")):
        raise ValueError("base_path cannot contain '.' or '..'")
    return f"/{stripped}"


class WebDisplay(DisplayAbstract):
    """Build a web interface and bridge browser input to agents."""

    def __init__(
        self,
        assets_dir: Path = DEFAULT_WEB_ASSETS,
        frontend_url: Optional[str] = None,
        expose_files: bool = False,
        max_events: int = 2000,
    ) -> None:
        super().__init__()
        self.assets_dir = assets_dir
        self.frontend_url = frontend_url
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

    def build_app(self) -> FastAPI:
        app = FastAPI(title="Xun Web", docs_url=None, redoc_url=None, lifespan=self._lifespan)
        app.include_router(self.build_routes())
        if self.assets_dir.is_dir() and not self.frontend_url:
            app.mount("/", StaticFiles(directory=self.assets_dir, html=True), name="web")
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
        async def config() -> dict[str, bool]:
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

        if self.frontend_url:
            frontend_url = self.frontend_url

            @router.get("/")
            async def frontend_redirect() -> RedirectResponse:
                return RedirectResponse(frontend_url)

        return router


class WebDisplayService:
    """Mount and serve one or more isolated web displays."""

    def __init__(self, host: str = "localhost", port: int = 18960, token: str = "") -> None:
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        self._displays: dict[str, WebDisplay] = {}
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._socket: Optional[socket.socket] = None
        self._started = threading.Event()
        self.app = FastAPI(docs_url=None, redoc_url=None, lifespan=self._lifespan)
        self._configure_login()
        self.app.add_middleware(_TokenAuthMiddleware, token=self.token, mounts=self._displays)

    def _configure_login(self) -> None:
        @self.app.get("/login", response_class=HTMLResponse)
        async def login_page(request: Request, next: str = "/") -> HTMLResponse:
            return self._login_response(request, next)

        @self.app.post("/login")
        async def login(
            request: Request,
            token: str = Form(...),
            next: str = Form("/"),
        ) -> Response:
            target = self._login_target(next)
            if not _tokens_match(token, self.token):
                return self._login_response(request, target, error="Invalid access token", status_code=401)
            root_path = request.scope.get("root_path", "").rstrip("/")
            response = RedirectResponse(f"{root_path}{target}" if root_path else target, status_code=303)
            response.set_cookie(
                _COOKIE_NAME,
                self.token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                path=f"{root_path}/" if root_path else "/",
            )
            return response

    def _login_target(self, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
            return self._default_path()
        path = parsed.path.rstrip("/")
        if not any(not mount or path == mount or path.startswith(f"{mount}/") for mount in self._displays):
            return self._default_path()
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    def _default_path(self) -> str:
        mount_path = next(iter(self._displays), "")
        return f"{mount_path}/" if mount_path else "/"

    def _login_response(
        self,
        request: Request,
        next_path: str,
        *,
        error: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        target = self._login_target(next_path)
        root_path = request.scope.get("root_path", "").rstrip("/")
        action = f"{root_path}/login" if root_path else "/login"
        return HTMLResponse(
            LOGIN_TEMPLATE.render(action=action, target=target, error=error),
            status_code=status_code,
        )

    def mount(self, path: str, display: WebDisplay) -> WebDisplayService:
        if self._started.is_set():
            raise RuntimeError("Cannot mount displays after the service has started")
        mount_path = _normalize_base_path(path)
        if mount_path in self._displays:
            raise ValueError(f"A display is already mounted at {mount_path or '/'}")
        if display in self._displays.values():
            raise ValueError("A WebDisplay can only be mounted once")
        if not mount_path and self._displays:
            raise ValueError("The root display must be the only mounted display")
        if mount_path and "" in self._displays:
            raise ValueError("Cannot add displays alongside a root display")
        if any(
            mount_path.startswith(f"{existing}/") or existing.startswith(f"{mount_path}/")
            for existing in self._displays
        ):
            raise ValueError("Display mount paths cannot overlap")
        self._displays[mount_path] = display
        self.app.mount(mount_path or "/", display.build_app())
        return self

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncGenerator[None, None]:
        loop = asyncio.get_running_loop()
        if any(display._loop is not None for display in self._displays.values()):
            raise RuntimeError("A mounted WebDisplay is already attached to a running app")
        for display in self._displays.values():
            display._attach(loop)
        self._started.set()
        try:
            yield
        finally:
            for display in self._displays.values():
                display._detach()
            self._started.clear()

    def access_url(self, path: str = "", _map_0000 = False) -> str:
        mount_path = _normalize_base_path(path)
        if mount_path not in self._displays:
            raise ValueError(f"No display mounted at {mount_path or '/'}")
        if _map_0000 and self.host == "0.0.0.0":
            host = "localhost"
        else:
            host = self.host
        url = f"http://{host}:{self.port}{mount_path}/"
        return f"{url}?token={quote(self.token)}"

    def start(self, *, blocking: bool = False) -> threading.Thread:
        if not self._displays:
            raise RuntimeError("Mount at least one WebDisplay before starting the service")
        if self._thread and self._thread.is_alive():
            return self._thread
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.host, self.port))
        self._socket.listen()
        self.port = self._socket.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(self.app, log_level="warning"))
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [self._socket]},
            daemon=True,
            name="xun-web-server",
        )
        self._thread.start()
        if not self._started.wait(timeout=5):
            self.stop()
            raise RuntimeError("WebDisplayService failed to start")
        print("Agents are available at the following URLs:")
        for path in self._displays:
            if self.host == "0.0.0.0":
                print(f"{self.access_url(path)} (aka {self.access_url(path, _map_0000=True)})")
            else:
                print(f"{self.access_url(path)}")
        if blocking:
            self._thread.join()
        return self._thread

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if self._socket:
            self._socket.close()
        self._server = None
        self._thread = None
        self._socket = None
