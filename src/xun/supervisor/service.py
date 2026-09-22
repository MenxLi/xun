from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import replace

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
from rich.console import Console

from .runtime import ContainerManager, ManagedContainer
from .users import UserStore

_console = Console(stderr=True)


class Supervisor:
    def __init__(self, store: UserStore, containers: ContainerManager, interval: float) -> None:
        self.store = store
        self.backend = containers
        self.interval = interval
        self.active: dict[str, ManagedContainer] = {}
        self.pruned = False

    async def _sync_paused(
        self, name: str, container: ManagedContainer, paused: bool
    ) -> ManagedContainer:
        if container.paused == paused:
            return container
        action = self.backend.pause if paused else self.backend.resume
        await asyncio.to_thread(action, container)
        state = "Paused" if paused else "Resumed"
        _console.print(f"[cyan]{state} container {container.id} for {name}.[/cyan]")
        return replace(container, paused=paused)

    async def reconcile(self) -> None:
        users = {user.name: user for user in await asyncio.to_thread(self.store.list)}
        for name, container in list(self.active.items()):
            user = users.get(name)
            unchanged = (
                user is not None
                and container.token == user.token
                and container.generation == user.generation
            )
            running = unchanged and await asyncio.to_thread(self.backend.is_running, container)
            if not running:
                reason = "user removed" if user is None else "stale token or generation" if not unchanged else "container exited"
                await asyncio.to_thread(self.backend.stop, container)
                del self.active[name]
                _console.print(f"[yellow]Stopped container for {name} ({reason}).[/yellow]")
            else:
                assert user is not None
                self.active[name] = await self._sync_paused(name, container, user.paused)

        for name, user in users.items():
            if name in self.active:
                continue
            try:
                adopted = await asyncio.to_thread(self.backend.adopt, user)
                if adopted is not None and (
                    adopted.token != user.token or adopted.generation != user.generation
                ):
                    _console.print(f"[yellow]Adopted container for {name} has a stale token or generation, recreating it.[/yellow]")
                    await asyncio.to_thread(self.backend.stop, adopted)
                    adopted = None
                if adopted is not None:
                    adopted = await self._sync_paused(name, adopted, user.paused)
                    self.active[name] = adopted
                    _console.print(f"[cyan]Adopted container {adopted.id} for {name}.[/cyan]")
                else:
                    container = await asyncio.to_thread(self.backend.start, user)
                    self.active[name] = container
                    _console.print(f"[green]Started container {container.id} for {name}.[/green]")
            except Exception as error:
                _console.print(f"[red]Failed to start container for {name}: {error}[/red]")

        if not self.pruned:
            keep = {c.id for c in self.active.values()}
            await asyncio.to_thread(self.backend.prune, keep)
            _console.print(f"[dim]Pruned stray containers (keeping {len(keep)} active).[/dim]")
            self.pruned = True

    async def run(self) -> None:
        while True:
            reconciliation = asyncio.create_task(self.reconcile())
            try:
                await asyncio.shield(reconciliation)
            except asyncio.CancelledError:
                await reconciliation
                raise
            except Exception as error:
                _console.print(f"[red]Failed to reconcile containers: {error}[/red]")
            await asyncio.sleep(self.interval)

    @asynccontextmanager
    async def lifecycle(self):
        task: asyncio.Task[None] | None = None
        try:
            await self.reconcile()
            task = asyncio.create_task(self.run())
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await asyncio.to_thread(self.backend.close)


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_WEBSOCKET_HEADERS = {
    "sec-websocket-accept",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
}


class Multiplexer:
    def __init__(
        self,
        supervisor: Supervisor,
        *,
        manage_supervisor: bool = True,
        websocket_heartbeat: float = 20.0,
    ) -> None:
        self.supervisor = supervisor
        self.manage_supervisor = manage_supervisor
        self.websocket_heartbeat = websocket_heartbeat
        self.client: ClientSession | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        app.cleanup_ctx.append(self._lifecycle)
        return app

    async def _lifecycle(self, _: web.Application):
        async with ClientSession(
            auto_decompress=False,
            timeout=ClientTimeout(total=None),
        ) as client:
            self.client = client
            try:
                if self.manage_supervisor:
                    async with self.supervisor.lifecycle():
                        yield
                else:
                    yield
            finally:
                self.client = None

    @staticmethod
    def _is_frontend_path(path: str) -> bool:
        parts = path.strip("/").split("/", 1)
        relative = parts[1] if len(parts) > 1 else ""
        root = relative.split("/", 1)[0]
        return relative in {"", "login"} or root in {"chat", "docs"}

    @classmethod
    def _paused_response(cls, request: web.Request) -> web.Response:
        if cls._is_frontend_path(request.path):
            return web.Response(
                status=503,
                content_type="text/html",
                text=(
                    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                    "<title>Temporarily unavailable</title></head><body>"
                    "<main><h1>Temporarily unavailable</h1>"
                    "<p>This service is paused. Please try again later or contact "
                    "the administrator if you need assistance.</p></main>"
                    "</body></html>"
                ),
                headers={"Retry-After": "60"},
            )
        return web.Response(status=503, text="Service paused", headers={"Retry-After": "60"})

    @staticmethod
    def _request_headers(request: web.Request, *, websocket: bool = False) -> list[tuple[str, str]]:
        blocked = _HOP_BY_HOP_HEADERS | {"host"}
        if websocket:
            blocked |= _WEBSOCKET_HEADERS
        headers = [
            (key, value)
            for key, value in request.headers.items()
            if key.lower() not in blocked
        ]
        headers += [
            ("X-Forwarded-Host", request.host),
            ("X-Forwarded-Proto", request.scheme),
        ]
        if request.remote:
            headers.append(("X-Forwarded-For", request.remote))
        return headers

    async def handle(self, request: web.Request) -> web.StreamResponse:
        name = request.path.lstrip("/").split("/", 1)[0]
        container = self.supervisor.active.get(name)
        if container is None:
            raise web.HTTPNotFound(text="User not found")
        if container.paused:
            return self._paused_response(request)
        target = f"127.0.0.1:{container.port}{request.raw_path}"
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._websocket(request, f"ws://{target}")
        return await self._http(request, f"http://{target}")

    async def _http(self, request: web.Request, target: str) -> web.StreamResponse:
        assert self.client is not None
        try:
            async with self.client.request(
                request.method,
                target,
                headers=self._request_headers(request),
                data=request.content.iter_any(),
                allow_redirects=False,
            ) as upstream:
                headers = [
                    (key, value)
                    for key, value in upstream.headers.items()
                    if key.lower() not in _HOP_BY_HOP_HEADERS
                ]
                if not 200 <= upstream.status < 300:
                    print(f"{request.method} {request.path} -> {upstream.status}")
                response = web.StreamResponse(
                    status=upstream.status,
                    reason=upstream.reason,
                    headers=headers,
                )
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except ClientError as error:
            raise web.HTTPBadGateway(text=f"Upstream unavailable: {error}") from error

    async def _websocket(self, request: web.Request, target: str) -> web.WebSocketResponse:
        assert self.client is not None
        protocols = [
            protocol.strip()
            for protocol in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
            if protocol.strip()
        ]
        try:
            upstream = await self.client.ws_connect(
                target,
                headers=self._request_headers(request, websocket=True),
                protocols=protocols,
                autoclose=True,
                autoping=True,
            )
        except ClientError as error:
            raise web.HTTPBadGateway(text=f"Upstream unavailable: {error}") from error

        downstream = web.WebSocketResponse(
            protocols=protocols,
            autoclose=True,
            autoping=True,
            heartbeat=self.websocket_heartbeat,
            compress=False,
        )
        await downstream.prepare(request)

        async def forward(source, destination) -> None:
            async for message in source:
                if message.type == WSMsgType.TEXT:
                    await destination.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await destination.send_bytes(message.data)
                elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break

        tasks = {
            asyncio.create_task(forward(downstream, upstream)),
            asyncio.create_task(forward(upstream, downstream)),
        }
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await upstream.close()
        await downstream.close()
        return downstream