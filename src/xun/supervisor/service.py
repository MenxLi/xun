from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager, suppress

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

from .runtime import ContainerManager, ManagedContainer
from .users import UserStore


class Supervisor:
    def __init__(self, store: UserStore, containers: ContainerManager, interval: float) -> None:
        self.store = store
        self.backend = containers
        self.interval = interval
        self.active: dict[str, ManagedContainer] = {}

    async def reconcile(self) -> None:
        users = {user.name: user for user in await asyncio.to_thread(self.store.list)}
        for name, container in list(self.active.items()):
            user = users.get(name)
            unchanged = user is not None and container.token == user.token
            running = unchanged and await asyncio.to_thread(self.backend.is_running, container)
            if not running:
                await asyncio.to_thread(self.backend.stop, container)
                del self.active[name]

        for name, user in users.items():
            if name not in self.active:
                try:
                    self.active[name] = await asyncio.to_thread(self.backend.start, user)
                except Exception as error:
                    print(f"Failed to start container for {name}: {error}", file=sys.stderr)

    async def run(self) -> None:
        while True:
            reconciliation = asyncio.create_task(self.reconcile())
            try:
                await asyncio.shield(reconciliation)
            except asyncio.CancelledError:
                await reconciliation
                raise
            except Exception as error:
                print(f"Failed to reconcile containers: {error}", file=sys.stderr)
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        for container in list(self.active.values()):
            try:
                await asyncio.to_thread(self.backend.stop, container)
            except Exception as error:
                print(f"Failed to stop container {container.id}: {error}", file=sys.stderr)
        self.active.clear()

    @asynccontextmanager
    async def lifecycle(self):
        task: asyncio.Task[None] | None = None
        try:
            await asyncio.to_thread(self.backend.cleanup)
            await self.reconcile()
            task = asyncio.create_task(self.run())
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await self.stop()
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
    def __init__(self, supervisor: Supervisor, *, manage_supervisor: bool = True) -> None:
        self.supervisor = supervisor
        self.manage_supervisor = manage_supervisor
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

    def _target(self, request: web.Request) -> str | None:
        name = request.path.lstrip("/").split("/", 1)[0]
        container = self.supervisor.active.get(name)
        if container is None:
            return None
        return f"127.0.0.1:{container.port}{request.raw_path}"

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
        target = self._target(request)
        if target is None:
            raise web.HTTPNotFound(text="User not found")
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

        downstream = web.WebSocketResponse(protocols=protocols, autoclose=True, autoping=True)
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