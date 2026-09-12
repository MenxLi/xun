"""Authenticated service for mounting and managing web display sessions."""

from __future__ import annotations

import asyncio
import hmac
import secrets
import socket
import threading
from contextlib import AbstractContextManager, ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Literal, Optional
from urllib.parse import quote, urlencode, urlsplit

import jinja2
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount

from ..config import ASSET_DIR
from .web_display import WebDisplay


LOGIN_TEMPLATE = jinja2.Environment(autoescape=True).from_string(
    (ASSET_DIR / "login.template.html").read_text(encoding="utf-8")
)
_COOKIE_NAME = "xun_web_token"
_DEFAULT_WEB_ASSETS = ASSET_DIR / "web"


class _TokenAuthMiddleware:
    def __init__(self, app: Any, token: str, base_path: str) -> None:
        self.app = app
        self.token = token
        self.base_path = base_path
        self.chat_path = f"{base_path}/chat"
        self.login_path = f"{base_path}/login"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        root_path = scope.get("root_path", "").rstrip("/")
        request_path = connection.url.path
        if root_path and request_path.startswith(root_path):
            request_path = request_path[len(root_path):] or "/"
        mount_path = request_path.rstrip("/")
        if scope["type"] == "http" and scope.get("method") in {"GET", "HEAD"} and mount_path == self.base_path:
            query = f"?{connection.url.query}" if connection.url.query else ""
            response = RedirectResponse(f"./chat/{query}")
            await response(scope, receive, send)
            return

        bearer = connection.headers.get("authorization", "")
        header_token = bearer[7:] if bearer.lower().startswith("bearer ") else ""
        cookie_token = connection.cookies.get(_COOKIE_NAME, "")
        if _tokens_match(header_token, self.token) or _tokens_match(cookie_token, self.token):
            await self.app(scope, receive, send)
            return

        if request_path == self.login_path:
            await self.app(scope, receive, send)
            return
        query_token = connection.query_params.get("token")
        if scope["type"] == "http" and mount_path == self.chat_path and _tokens_match(query_token or "", self.token):
            query = urlencode([
                (key, value)
                for key, value in connection.query_params.multi_items()
                if key != "token"
            ])
            response = RedirectResponse(f"./?{query}" if query else "./", status_code=303)
            response.set_cookie(
                _COOKIE_NAME,
                self.token,
                httponly=True,
                samesite="strict",
                secure=connection.url.scheme == "https",
                path=f"{root_path}{self.base_path}/" or "/",
            )
            await response(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        elif self._is_display_page_request(scope, request_path):
            login_path = f"{root_path}{self.login_path}"
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
        request_path: str,
    ) -> bool:
        if scope.get("method") not in {"GET", "HEAD"}:
            return False
        return request_path.rstrip("/") == self.chat_path


def _tokens_match(value: str, expected: str) -> bool:
    return bool(value) and hmac.compare_digest(value, expected)


def _normalize_path(value: str, name: str = "path") -> str:
    stripped = value.strip().strip("/")
    if not stripped:
        return ""
    if any(part in {"", ".", ".."} for part in stripped.split("/")):
        raise ValueError(f"{name} cannot contain empty, '.' or '..' segments")
    return f"/{stripped}"


SessionStatus = Literal["idle", "running", "waiting"]


class SessionInfo(BaseModel):
    path: str
    name: str
    status: SessionStatus


class SessionList(BaseModel):
    sessions: list[SessionInfo]
    can_manage: bool


class SessionCreate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)


@dataclass
class _DisplaySession:
    display: WebDisplay
    name: str
    route: Mount
    context: Optional[ExitStack] = None


class WebDisplayService:
    """Mount and serve one or more isolated web displays."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 18960,
        token: str = "",
        base_path: str = "",
        assets_dir: Path = _DEFAULT_WEB_ASSETS,
        session_manager: Optional[Callable[[], AbstractContextManager[tuple[str, WebDisplay]]]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        self.base_path = _normalize_path(base_path, "base_path")
        self.chat_path = f"{self.base_path}/chat"
        self.session_path = f"{self.base_path}/session"
        self.sessions_api_path = f"{self.base_path}/api/sessions"
        self.login_path = f"{self.base_path}/login"
        self._sessions: dict[str, _DisplaySession] = {}
        self._session_manager = session_manager
        self._session_lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._socket: Optional[socket.socket] = None
        self._started = threading.Event()
        self.app = FastAPI(docs_url=None, redoc_url=None, lifespan=self._lifespan)
        self._configure_login()
        self._configure_sessions()
        self._configure_chat(assets_dir)
        self.app.add_middleware(_TokenAuthMiddleware, token=self.token, base_path=self.base_path)

    def _configure_chat(self, assets_dir: Path) -> None:
        if assets_dir.is_dir():
            self.app.mount(self.chat_path, StaticFiles(directory=assets_dir, html=True), name="chat")

    def _configure_sessions(self) -> None:
        @self.app.get(self.sessions_api_path)
        async def sessions() -> SessionList:
            return SessionList(sessions=self.list_sessions(), can_manage=self._session_manager is not None)

        @self.app.get(f"{self.sessions_api_path}/{{session_path:path}}")
        async def session(session_path: str) -> SessionInfo:
            mount_path = _normalize_path(session_path)
            with self._session_lock:
                if mount_path not in self._sessions:
                    raise HTTPException(404, "Session not found")
                return self._session_info(mount_path)

        @self.app.post(self.sessions_api_path, status_code=201)
        async def create_session(request: SessionCreate) -> SessionInfo:
            manager = self._session_manager
            if manager is None:
                raise HTTPException(405, "Session management is disabled")
            context = ExitStack()
            try:
                raw_mount_path, display = context.enter_context(manager())
                mount_path = _normalize_path(raw_mount_path)
                self.mount(mount_path, display, name=request.name)
                with self._session_lock:
                    self._sessions[mount_path].context = context
            except BaseException:
                context.close()
                raise
            return self._session_info(mount_path)

        @self.app.delete(f"{self.sessions_api_path}/{{session_path:path}}")
        async def remove_session(session_path: str) -> dict[str, bool]:
            if self._session_manager is None:
                raise HTTPException(405, "Session management is disabled")
            if len(self._sessions) <= 1:
                raise HTTPException(409, "The last session cannot be removed")
            self.unmount(session_path)
            return {"removed": True}

    def _configure_login(self) -> None:
        @self.app.get(self.login_path, response_class=HTMLResponse)
        async def login_page(request: Request, next: str = "/") -> HTMLResponse:
            return self._login_response(request, next)

        @self.app.post(self.login_path)
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
                path=f"{root_path}{self.base_path}/" or "/",
            )
            return response

    def _login_target(self, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
            return self._default_path()
        if parsed.path.rstrip("/") != self.chat_path:
            return self._default_path()
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")

    def _default_path(self) -> str:
        return f"{self.chat_path}/"

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
        action = f"{root_path}{self.login_path}"
        return HTMLResponse(
            LOGIN_TEMPLATE.render(action=action, target=target, error=error),
            status_code=status_code,
        )

    def mount(self, path: str, display: WebDisplay, *, name: Optional[str] = None) -> WebDisplayService:
        mount_path = _normalize_path(path)
        session_name = (name or "").strip() or mount_path.rsplit("/", 1)[-1] or "Session"
        with self._session_lock:
            if mount_path in self._sessions:
                raise ValueError(f"A display is already mounted at {mount_path or '/'}")
            if any(session.display is display for session in self._sessions.values()):
                raise ValueError("A WebDisplay can only be mounted once")
            if any(
                mount_path.startswith(f"{existing}/") or existing.startswith(f"{mount_path}/")
                for existing in self._sessions
                if mount_path and existing
            ):
                raise ValueError("Display mount paths cannot overlap")
            display_app = display.build_app()
            if self._loop is not None:
                display._attach(self._loop)
            route_path = f"{self.session_path}{mount_path}"
            try:
                self.app.mount(route_path, display_app, name=f"session:{mount_path}")
            except BaseException:
                if self._loop is not None:
                    display._detach()
                raise
            route = self.app.routes[-1]
            assert isinstance(route, Mount)
            root_session = self._sessions.get("")
            if mount_path and root_session is not None:
                self.app.routes.remove(route)
                self.app.routes.insert(self.app.routes.index(root_session.route), route)
            self._sessions[mount_path] = _DisplaySession(display, session_name, route)
        return self

    def unmount(self, path: str) -> None:
        mount_path = _normalize_path(path)
        with self._session_lock:
            session = self._sessions.pop(mount_path, None)
            if session is None:
                raise HTTPException(404, "Session not found")
            self.app.routes.remove(session.route)
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(session.display._close_clients(), self._loop)
                session.display._detach()
        if session.context is not None:
            session.context.close()

    def list_sessions(self) -> list[SessionInfo]:
        with self._session_lock:
            return [self._session_info(path) for path in self._sessions]

    def _session_info(self, mount_path: str) -> SessionInfo:
        session = self._sessions[mount_path]
        agents = session.display.agents.values()
        if session.display._pending.list():
            status: SessionStatus = "waiting"
        elif any(agent.is_running for agent in agents):
            status = "running"
        else:
            status = "idle"
        return SessionInfo(path=mount_path or "/", name=session.name, status=status)

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncGenerator[None, None]:
        loop = asyncio.get_running_loop()
        with self._session_lock:
            displays = [session.display for session in self._sessions.values()]
            if any(display._loop is not None for display in displays):
                raise RuntimeError("A mounted WebDisplay is already attached to a running app")
            for display in displays:
                display._attach(loop)
            self._loop = loop
        self._started.set()
        try:
            yield
        finally:
            contexts: list[ExitStack] = []
            with self._session_lock:
                self._loop = None
                for session in self._sessions.values():
                    session.display._detach()
                for path, session in tuple(self._sessions.items()):
                    if session.context is None:
                        continue
                    self._sessions.pop(path)
                    self.app.routes.remove(session.route)
                    contexts.append(session.context)
            self._started.clear()
            for context in contexts:
                context.close()

    def access_url(self, path: str = "", _map_0000: bool = False) -> str:
        mount_path = _normalize_path(path)
        if mount_path not in self._sessions:
            raise ValueError(f"No display mounted at {mount_path or '/'}")
        host = "localhost" if _map_0000 and self.host == "0.0.0.0" else self.host
        session = mount_path or "/"
        query = urlencode({"session": session, "token": self.token}, quote_via=quote)
        return f"http://{host}:{self.port}{self.chat_path}/?{query}"

    def start(self, *, blocking: bool = False) -> threading.Thread:
        if not self._sessions:
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
        for path in self._sessions:
            if self.host == "0.0.0.0":
                print(f"{self.access_url(path)} (aka {self.access_url(path, _map_0000=True)})")
            else:
                print(self.access_url(path))
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
