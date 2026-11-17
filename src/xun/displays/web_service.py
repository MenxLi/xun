"""Authenticated service for mounting and managing web display sessions."""

from __future__ import annotations

import asyncio
import hmac
import re
import secrets
import socket
import threading
import time
from contextlib import AbstractContextManager, ExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, cast, Literal, Optional, Protocol, TypedDict
from urllib.parse import quote, urlencode, urlsplit

import jinja2
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount
from starlette.types import Message, Scope

from ..core.config import ASSET_DIR
from .web_display import WebDisplay


@cache
def _login_template() -> jinja2.Template:
    return jinja2.Environment(autoescape=True).from_string(
        (ASSET_DIR / "login.template.html").read_text(encoding="utf-8")
    )


_COOKIE_NAME = "xun_web_token"
_DEFAULT_WEB_ASSETS = ASSET_DIR / "web"
_DEFAULT_DOCS_ASSETS = ASSET_DIR / "docs"


class _TokenAuthMiddleware:
    # '<session>/srv/<key>/...' urls are self-authenticating: the key is the
    # credential and its mount vanishes on stop or expiry, so the prefix is
    # safe to open as long as sessions may not be named 'srv' (see mount()).
    def __init__(
        self,
        app: Any,
        token: str,
        base_path: str,
        session_path: str,
        docs_path: str,
    ) -> None:
        self.app = app
        self.token = token
        self.base_path = base_path
        self.serve_prefix = re.compile(rf"^{re.escape(session_path)}(?:/.*)?/srv/")
        self.docs_path = docs_path
        self.chat_path = f"{base_path}/chat"
        self.login_path = f"{base_path}/login"
        self.logout_path = f"{base_path}/logout"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        async def log_non_success_response(message: Message) -> None:
            if message["type"] == "http.response.start" and not 200 <= message["status"] < 300:
                print(f"{scope.get('method', '')} {scope.get('path', '')} -> {message['status']}")
            await send(message)

        connection = HTTPConnection(scope)
        root_path = scope.get("root_path", "").rstrip("/")
        request_path = connection.url.path
        if root_path and request_path.startswith(root_path):
            request_path = request_path[len(root_path):] or "/"
        mount_path = request_path.rstrip("/")
        if scope["type"] == "http" and scope.get("method") in {"GET", "HEAD"} and mount_path == self.base_path:
            query = f"?{connection.url.query}" if connection.url.query else ""
            response = RedirectResponse(f"{root_path}{self.chat_path}/{query}")
            await response(scope, receive, log_non_success_response)
            return

        bearer = connection.headers.get("authorization", "")
        header_token = bearer[7:] if bearer.lower().startswith("bearer ") else ""
        cookie_token = connection.cookies.get(_COOKIE_NAME, "")
        if _tokens_match(header_token, self.token) or _tokens_match(cookie_token, self.token):
            await self.app(scope, receive, log_non_success_response)
            return

        if scope["type"] == "http" and scope.get("method") in {"GET", "HEAD"} and self.serve_prefix.match(request_path):
            await self.app(scope, receive, log_non_success_response)
            return

        if scope["type"] == "http" and scope.get("method") in {"GET", "HEAD"} and (
            request_path == self.docs_path or request_path.startswith(f"{self.docs_path}/")
        ):
            await self.app(scope, receive, log_non_success_response)
            return

        if request_path in {self.login_path, self.logout_path}:
            await self.app(scope, receive, log_non_success_response)
            return
        query_token = connection.query_params.get("token")
        if scope["type"] == "http" and mount_path == self.chat_path and _tokens_match(query_token or "", self.token):
            query = urlencode([
                (key, value)
                for key, value in connection.query_params.multi_items()
                if key != "token"
            ])
            response = RedirectResponse(
                f"{root_path}{self.chat_path}/?{query}" if query else f"{root_path}{self.chat_path}/",
                status_code=303,
            )
            response.set_cookie(
                _COOKIE_NAME,
                self.token,
                httponly=True,
                samesite="strict",
                secure=connection.url.scheme == "https",
                path=f"{root_path}{self.base_path}/" or "/",
            )
            await response(scope, receive, log_non_success_response)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        elif self._is_display_page_request(scope, request_path):
            login_path = f"{root_path}{self.login_path}"
            target = request_path
            if connection.url.query:
                target = f"{target}?{connection.url.query}"
            response = RedirectResponse(f"{login_path}?{urlencode({'next': target})}", status_code=303)
            await response(scope, receive, log_non_success_response)
        else:
            response = JSONResponse({"detail": "Not authenticated"}, status_code=401)
            await response(scope, receive, log_non_success_response)

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


SessionStatus = Literal["asleep", "idle", "running", "waiting"]


class SessionInfo(BaseModel):
    path: str
    name: str
    status: SessionStatus


class SessionList(BaseModel):
    sessions: list[SessionInfo]
    can_manage: bool
    can_fork: bool


class SessionCreate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)


class SessionRemove(BaseModel):
    path: str


class SessionFork(BaseModel):
    path: str
    name: Optional[str] = Field(default=None, max_length=80)


class SessionRename(BaseModel):
    path: str
    name: str = Field(max_length=80)


class WebSession(Protocol):
    """A mountable session: mounted under `mount_path`. `destroy()` on removal,
    `close()` on shutdown, `save_meta()` to flush changes such as renames.

    Sessions may rest (see Residency): `display_alive` peeks without
    waking, `offload()` releases the display (a no-op for eager sessions);
    `fork()` copies under a fresh id, raising ValueError when impossible."""

    name: str

    @property
    def mount_path(self) -> str: ...

    @property
    def display(self) -> WebDisplay: ...

    @property
    def display_alive(self) -> Optional[WebDisplay]: ...

    def offload(self) -> None: ...

    def save_meta(self) -> None: ...

    def fork(self, name: Optional[str] = None) -> "WebSession": ...

    def close(self) -> None: ...

    def destroy(self) -> None: ...


SessionSource = Callable[[], "WebSession | AbstractContextManager[tuple[str, WebDisplay]]"]


@dataclass
class SimpleSession:
    """Session over a ready-made display: close == destroy == `release()` once."""

    mount_path: str
    display: WebDisplay
    name: str = ""
    release: Optional[Callable[[], object]] = None
    _closed = False

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.mount_path.rsplit("/", 1)[-1] or "Session"

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self.release is not None:
                self.release()

    def destroy(self) -> None:
        self.close()

    @property
    def display_alive(self) -> Optional[WebDisplay]:
        return self.display  # eager: never rests

    def offload(self) -> None:
        pass  # a ready-made display cannot rest

    def save_meta(self) -> None:
        pass  # nothing outlives the process

    def fork(self, name: Optional[str] = None) -> "SimpleSession":
        raise ValueError("Ephemeral sessions cannot be forked")


class _SessionDispatcher:
    """ASGI app behind each mount: resolves `session.display` per request and
    caches the built app per display generation (a display's routes close over it)."""

    def __init__(self, service: "WebDisplayService", mount_path: str) -> None:
        self._service = service
        self.mount_path = mount_path
        self._app: Optional[Any] = None
        self._app_display: Optional[WebDisplay] = None
        self.last_used = time.monotonic()
        self.in_flight = 0  # handshakes included: a resolving request pins its display

    def cached_app(self, display: WebDisplay) -> Optional[Any]:
        return self._app if self._app is not None and self._app_display is display else None

    def rebind(self, app: Any, display: WebDisplay) -> None:
        self._app = app
        self._app_display = display

    def invalidate(self) -> None:
        self._app = None
        self._app_display = None  # the cached app's routes hold the display's agents

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        # materializing a resting session (factory, snapshots) is blocking work:
        # run it on a worker thread so the event loop keeps serving other sessions.
        self.in_flight += 1
        try:
            app = await asyncio.to_thread(self._service._mount_app, self, asyncio.get_running_loop())
            if app is None:
                await JSONResponse({"detail": "Session not found"}, status_code=404)(scope, receive, send)
                return
            await app(scope, receive, send)
        finally:
            self.in_flight -= 1


@dataclass
class _Mounted:
    session: WebSession
    route: Mount
    dispatcher: _SessionDispatcher
    managed: bool = False  # factory-created: the service closes it on shutdown

class Residency(TypedDict):
    """Residency policy for restable sessions"""
    max_awake: int  # awake cap: beyond it, least recently served rest first
    idle_timeout: float  # awake and idle past this (seconds) rest too, cap or not


class WebDisplayService:
    """Mount and serve one or more isolated web displays."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 18960,
        token: str = "",
        base_path: str = "",
        assets_dir: Path = _DEFAULT_WEB_ASSETS,
        docs_dir: Path = _DEFAULT_DOCS_ASSETS,
        session_factory: Optional[SessionSource] = None,
        can_fork: bool = False,
        residency: Residency = Residency(max_awake=16, idle_timeout=24 * 3600),
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        self.base_path = _normalize_path(base_path, "base_path")
        self.chat_path = f"{self.base_path}/chat"
        self.docs_path = f"{self.base_path}/docs"
        self.session_path = f"{self.base_path}/session"
        self.sessions_api_path = f"{self.base_path}/api/sessions"
        self.login_path = f"{self.base_path}/login"
        self.logout_path = f"{self.base_path}/logout"
        self._sessions: dict[str, _Mounted] = {}
        self._session_factory = session_factory
        self._can_fork = can_fork
        self._residency = residency
        self._session_lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._socket: Optional[socket.socket] = None
        self._started = threading.Event()
        self.app = FastAPI(docs_url=None, redoc_url=None, lifespan=self._lifespan)
        # Compress the large JSON event listing; skip small and already-
        # compressed (images, zips, PDFs, ...) payloads.
        self.app.add_middleware(
            GZipMiddleware,
            minimum_size=1024,
            compresslevel=6,
            exclude_content_types=(
                "application/gzip",
                "application/x-gzip",
                "application/zip",
                "application/octet-stream",
                "application/pdf",
                "audio/*",
                "font/woff",
                "font/woff2",
                "image/avif",
                "image/gif",
                "image/jpeg",
                "image/png",
                "image/webp",
                "text/event-stream",
                "video/*",
            ),
        )
        self._configure_login()
        self._configure_sessions()
        self._configure_chat(assets_dir)
        self._configure_docs(docs_dir)
        self.app.add_middleware(
            _TokenAuthMiddleware,
            token=self.token,
            base_path=self.base_path,
            session_path=self.session_path,
            docs_path=self.docs_path,
        )

    def _configure_chat(self, assets_dir: Path) -> None:
        if assets_dir.is_dir():
            self.app.mount(self.chat_path, StaticFiles(directory=assets_dir, html=True), name="chat")

    def _configure_docs(self, docs_dir: Path) -> None:
        if (docs_dir / "index.html").is_file():
            @self.app.api_route(self.docs_path, methods=["GET", "HEAD"], include_in_schema=False)
            async def docs_root(request: Request) -> RedirectResponse:
                root_path = request.scope.get("root_path", "").rstrip("/")
                return RedirectResponse(f"{root_path}{self.docs_path}/")

            self.app.mount(self.docs_path, StaticFiles(directory=docs_dir, html=True), name="docs")

    def _configure_sessions(self) -> None:
        @self.app.get(self.sessions_api_path)
        async def sessions() -> SessionList:
            # the frontend polls this route: sweeps promptly after user activity
            await asyncio.to_thread(self._enforce_residency)
            return SessionList(
                sessions=self.list_sessions(),
                can_manage=self._session_factory is not None,
                can_fork=self._can_fork,
            )

        @self.app.get(f"{self.sessions_api_path}/{{session_path:path}}")
        async def session(session_path: str) -> SessionInfo:
            mount_path = _normalize_path(session_path)
            with self._session_lock:
                if mount_path not in self._sessions:
                    raise HTTPException(404, "Session not found")
                return self._session_info(mount_path)

        @self.app.post(f"{self.sessions_api_path}/create", status_code=201)
        async def create_session(request: SessionCreate) -> SessionInfo:
            factory = self._session_factory
            if factory is None:
                raise HTTPException(405, "Session management is disabled")
            session = self._open_session(factory)
            try:
                if name := (request.name or "").strip():
                    session.name = name
                    session.save_meta()
                self.mount(session, managed=True)
            except BaseException:
                session.destroy()
                raise
            return self._session_info(_normalize_path(session.mount_path))

        @self.app.post(f"{self.sessions_api_path}/remove")
        async def remove_session(request: SessionRemove) -> dict[str, bool]:
            if self._session_factory is None:
                raise HTTPException(405, "Session management is disabled")
            if len(self._sessions) <= 1:
                raise HTTPException(409, "The last session cannot be removed")
            self.unmount(request.path)
            return {"removed": True}

        @self.app.post(f"{self.sessions_api_path}/fork", status_code=201)
        async def fork_session(request: SessionFork) -> SessionInfo:
            if not self._can_fork:
                raise HTTPException(405, "Session forking is disabled")
            with self._session_lock:
                mounted = self._sessions.get(_normalize_path(request.path))
            if mounted is None:
                raise HTTPException(404, "Session not found")
            source = mounted.session
            live = source.display_alive
            if live is not None and (live.has_waiting_prompts() or live.has_running_agents()):
                raise HTTPException(409, "Cannot fork a busy session")  # a resting session is never busy
            try:  # copytree, db backup, and waking the copy are blocking work
                session = await asyncio.to_thread(source.fork, (request.name or "").strip() or None)
            except ValueError as e:
                raise HTTPException(409, str(e))
            try:
                self.mount(session, managed=True)
            except BaseException:
                session.destroy()
                raise
            return self._session_info(_normalize_path(session.mount_path))

        @self.app.post(f"{self.sessions_api_path}/rename")
        async def rename_session(request: SessionRename) -> SessionInfo:
            if self._session_factory is None:
                raise HTTPException(405, "Session management is disabled")
            return self.rename(request.path, request.name)

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
            if not _tokens_match(token.strip(), self.token):
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

        @self.app.post(self.logout_path)
        async def logout(request: Request) -> RedirectResponse:
            root_path = request.scope.get("root_path", "").rstrip("/")
            response = RedirectResponse(f"{root_path}{self.login_path}", status_code=303)
            response.delete_cookie(_COOKIE_NAME, path=f"{root_path}{self.base_path}/" or "/")
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
            _login_template().render(action=action, target=target, error=error),
            status_code=status_code,
        )

    def _open_session(self, factory: SessionSource) -> WebSession:
        source = factory()
        if not isinstance(source, AbstractContextManager):
            return cast(WebSession, source)
        stack = ExitStack()
        try:
            mount_path, display = stack.enter_context(source)
        except BaseException:
            stack.close()
            raise
        return SimpleSession(mount_path, display, release=stack.close)

    def mount(self, session: WebSession, *, managed: bool = False) -> WebDisplayService:
        """Mount without touching the display: the dispatcher resolves it per request."""
        mount_path = _normalize_path(session.mount_path)
        if "srv" in mount_path.split("/"):
            raise ValueError("Session paths cannot contain a 'srv' segment")  # see _TokenAuthMiddleware
        with self._session_lock:
            if mount_path in self._sessions:
                raise ValueError(f"A display is already mounted at {mount_path or '/'}")
            live = session.display_alive
            if live is not None and any(
                mounted.session.display_alive is live for mounted in self._sessions.values()
            ):
                raise ValueError("A WebDisplay can only be mounted once")
            if any(
                mount_path.startswith(f"{existing}/") or existing.startswith(f"{mount_path}/")
                for existing in self._sessions
                if mount_path and existing
            ):
                raise ValueError("Display mount paths cannot overlap")
            dispatcher = _SessionDispatcher(self, mount_path)
            route_path = f"{self.session_path}{mount_path}"
            self.app.mount(route_path, dispatcher, name=f"session:{mount_path}")
            route = self.app.routes[-1]
            assert isinstance(route, Mount)
            root_session = self._sessions.get("")
            if mount_path and root_session is not None:
                self.app.routes.remove(route)
                self.app.routes.insert(self.app.routes.index(root_session.route), route)
            self._sessions[mount_path] = _Mounted(session, route, dispatcher, managed)
        return self

    def unmount(self, path: str) -> None:
        mount_path = _normalize_path(path)
        with self._session_lock:
            mounted = self._sessions.pop(mount_path, None)
            if mounted is None:
                raise HTTPException(404, "Session not found")
            self.app.routes.remove(mounted.route)
            display = mounted.session.display_alive
            if self._loop is not None and display is not None:
                asyncio.run_coroutine_threadsafe(display._close_clients(), self._loop)
                display._detach()
            mounted.dispatcher.invalidate()
        mounted.session.destroy()

    def rename(self, path: str, name: str) -> SessionInfo:
        mount_path = _normalize_path(path)
        session_name = name.strip()
        if not session_name:
            raise HTTPException(422, "Session name cannot be empty")
        with self._session_lock:
            mounted = self._sessions.get(mount_path)
            if mounted is None:
                raise HTTPException(404, "Session not found")
            mounted.session.name = session_name
            mounted.session.save_meta()
            return self._session_info(mount_path)

    def list_sessions(self) -> list[SessionInfo]:
        with self._session_lock:
            # newest first: mount order is birth order (restore sorts by created_at, create appends)
            return [self._session_info(path) for path in reversed(self._sessions)]

    def _session_info(self, mount_path: str) -> SessionInfo:
        mounted = self._sessions[mount_path]
        live = mounted.session.display_alive
        if live is None:
            status: SessionStatus = "asleep"  # resting: wakes on the next request
        elif live.has_waiting_prompts():
            status = "waiting"
        elif live.has_running_agents():
            status = "running"
        else:
            status = "idle"
        return SessionInfo(path=mount_path or "/", name=mounted.session.name, status=status)

    def _mount_app(self, dispatcher: "_SessionDispatcher", loop: asyncio.AbstractEventLoop) -> Optional[Any]:
        """Resolve the dispatcher's app, materializing a resting session. None once the mount is gone."""
        with self._session_lock:
            mounted = self._sessions.get(dispatcher.mount_path)
            if mounted is None or mounted.dispatcher is not dispatcher:
                dispatcher.invalidate()
                return None
            display = mounted.session.display
            dispatcher.last_used = time.monotonic()
            app = dispatcher.cached_app(display)
            if app is None:
                loop = self._loop or loop
                if display._loop not in (None, loop):
                    raise RuntimeError("A mounted WebDisplay is already attached to a running app")
                if display._loop is None:
                    display._attach(loop)  # lazy attach: the service loop, or this request's
                app = display.build_app()
                dispatcher.rebind(app, display)
            return app

    def _enforce_residency(self) -> None:
        """Rest awake sessions idle past the timeout, and beyond the
        cap the least recently served go first. Served, watched, waiting, or running
        sessions are exempt and keep their slot."""
        config = self._residency
        deadline = time.monotonic() - config["idle_timeout"]
        with self._session_lock:
            # one LRU-sorted pass covers both limits: past a fresh session within
            # the cap, everything remaining is fresher and safe
            resident = sorted(
                (m for m in self._sessions.values() if m.session.display_alive is not None),
                key=lambda m: m.dispatcher.last_used,
            )
            excess = len(resident) - config["max_awake"]
            for mounted in resident:
                if excess <= 0 and mounted.dispatcher.last_used > deadline:
                    break
                session = mounted.session
                display = session.display_alive  # recheck: it may have ended since the census
                if display is None:
                    continue
                if (mounted.dispatcher.in_flight or display.has_viewers()
                        or display.has_waiting_prompts() or display.has_running_agents()):
                    continue
                session.offload()
                if session.display_alive is not None:
                    continue  # an eager session cannot rest: it keeps its slot
                mounted.dispatcher.invalidate()
                if self._loop is not None:
                    asyncio.run_coroutine_threadsafe(display._close_clients(), self._loop)
                    display._detach()  # releases serve timers and executors
                excess -= 1

    async def _residency_sweeper(self) -> None:
        """Background sweeps so the idle timeout bites even while no frontend polls."""
        interval = max(1.0, min(60.0, self._residency["idle_timeout"] / 4))
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(self._enforce_residency)

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncGenerator[None, None]:
        # displays attach to the loop lazily, on first request: resting sessions
        # must not be woken just because the service started.
        self._loop = asyncio.get_running_loop()
        sweeper = asyncio.create_task(self._residency_sweeper())
        self._started.set()
        try:
            yield
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
            sessions: list[WebSession] = []
            with self._session_lock:
                self._loop = None
                for mounted in self._sessions.values():
                    live = mounted.session.display_alive
                    if live is not None:
                        live._detach()
                        mounted.dispatcher.invalidate()
                for path, mounted in tuple(self._sessions.items()):
                    if not mounted.managed:
                        continue
                    self._sessions.pop(path)
                    self.app.routes.remove(mounted.route)
                    sessions.append(mounted.session)
            self._started.clear()
            for session in sessions:
                session.close()

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
