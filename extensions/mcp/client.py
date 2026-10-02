"""stdio MCP client: newline-delimited JSON-RPC 2.0 over a child process.

Stdlib only. Everything about transport and connections lives here; every xun-facing
decision (naming, schemas, results, reconciliation) lives in setup_extension.py.

Only what v1 needs is implemented: `initialize`, `tools/list` (paginated), `tools/call`.
Server->client requests and list-change notifications are ignored on purpose, see README.
"""
from __future__ import annotations

import asyncio
import atexit
import itertools
import json
import os
import threading
import time
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "xun-mcp", "version": "0.1"}
MAX_MESSAGE_SIZE = 16 * 1024 * 1024


class McpError(RuntimeError):
    """Any transport or protocol failure; carries a message meant for the model."""


class StdioServer:
    """One child process; requests multiplexed by id, notifications ignored.

    The constructor doubles as the config surface: keys under `servers.<name>` in the
    extension settings map straight onto these parameters.
    """

    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: str | None = None,
                 init_timeout: float = 20.0) -> None:
        self.name, self.command, self.args = name, command, args or []
        self.env, self.cwd, self.init_timeout = env, cwd, init_timeout
        self.tools: list[dict] = []
        self._ids = itertools.count(1)
        self._pending: dict[Any, asyncio.Future] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._read_error: McpError | None = None

    async def start(self) -> None:
        env = {**os.environ, **self.env} if self.env else None
        self._proc = await asyncio.create_subprocess_exec(
            self.command, *self.args, env=env, cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=MAX_MESSAGE_SIZE)
        asyncio.create_task(self._read_loop())
        await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": CLIENT_INFO}, self.init_timeout)
        await self._notify("notifications/initialized")
        self.tools = await self.list_tools()

    async def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        for _ in range(100):                        # the spec allows paginated tools/list
            res = await self._request("tools/list", {"cursor": cursor} if cursor else {},
                                      self.init_timeout)
            tools += res.get("tools", [])
            if not (cursor := res.get("nextCursor")):
                break
        return tools

    async def call_tool(self, tool: str, arguments: dict, timeout: float) -> dict:
        return await self._request("tools/call", {"name": tool, "arguments": arguments},
                                   timeout)

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while (line := await self._proc.stdout.readline()):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue                        # tolerate stray log output on stdout
                if not isinstance(msg, dict):
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception as exc:
            self._read_error = McpError(f"MCP server '{self.name}' read failed: {exc}")
        finally:
            if self._read_error is None:
                self._read_error = McpError(f"MCP server '{self.name}' exited")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(self._read_error)
            self._pending.clear()

    async def _request(self, method: str, params: dict | None, timeout: float) -> Any:
        if self._read_error is not None:
            raise self._read_error
        if self._proc is None or self._proc.stdin is None or self._proc.returncode is not None:
            raise McpError(f"MCP server '{self.name}' is not running")
        rid = next(self._ids)
        self._pending[rid] = fut = asyncio.get_running_loop().create_future()
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._proc.stdin.write((json.dumps(payload) + "\n").encode())
            await self._proc.stdin.drain()
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise McpError(f"'{method}' on '{self.name}' timed out after {timeout}s")
        except OSError as exc:
            self._read_error = McpError(f"MCP server '{self.name}' write failed: {exc}")
            if not fut.done():
                fut.cancel()
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(self._read_error)
            raise self._read_error from exc
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            err = msg["error"]
            raise McpError(f"{method} failed: {err.get('code')} {err.get('message')}")
        return msg.get("result")

    async def _notify(self, method: str) -> None:
        if self._proc and self._proc.stdin:
            self._proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())
            await self._proc.stdin.drain()

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()


class Manager:
    """Process-wide: one loop thread (xun's API is synchronous), one entry per server.

    Extension modules are imported once per process while `setup_extension` replays for
    every agent, so connections must not be owned by an agent.
    """

    RETRY_AFTER = 30.0
    """Backoff after a failed start, so one broken server is not paid for per agent."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._server_guard = threading.Lock()
        self._servers: dict[str, StdioServer] = {}
        self._failed: dict[str, float] = {}

    def _run(self, coro: Any, timeout: float | None = None) -> Any:
        with self._guard:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._loop.run_forever,
                                                name="mcp-io", daemon=True)
                self._thread.start()
                atexit.register(self.shutdown)
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def ensure(self, name: str, cfg: dict, refresh: bool = False) -> list[dict]:
        """Start the server if needed, then return its tools."""
        with self._server_guard:
            return self._ensure(name, cfg, refresh)

    def _ensure(self, name: str, cfg: dict, refresh: bool = False) -> list[dict]:
        srv = self._servers.get(name)
        exited = srv is not None and srv._proc is not None and srv._proc.returncode is not None
        if srv is not None and (srv._read_error is not None or exited):
            self._servers.pop(name)
            self._run(srv.stop(), 8)
            srv = None
        if srv is not None:
            if refresh:
                srv.tools = self._run(srv.list_tools(), srv.init_timeout + 10)
            return srv.tools
        when = self._failed.get(name)
        if when is not None and time.monotonic() - when < self.RETRY_AFTER:
            raise McpError(f"'{name}' is in backoff after a failed start")
        try:
            srv = StdioServer(name, **cfg)          # a typo'd config key is a config error,
            self._run(srv.start(), srv.init_timeout + 10)   # not a reason to lose the rest
        except Exception as exc:
            if srv is not None:
                self._run(srv.stop(), 8)
            self._failed[name] = time.monotonic()
            raise McpError(f"'{name}' failed to start: {exc}") from exc
        self._servers[name] = srv
        self._failed.pop(name, None)
        return srv.tools

    def restart(self, name: str, cfg: dict) -> list[dict]:
        """Replace even a hung process; explicit restarts bypass start backoff."""
        with self._server_guard:
            srv = self._servers.pop(name, None)
            if srv is not None:
                self._run(srv.stop(), 8)
            self._failed.pop(name, None)
            return self._ensure(name, cfg)

    def status(self, name: str) -> str:
        """Report the last known state without contacting or starting the server."""
        srv = self._servers.get(name)
        if srv is not None:
            if srv._read_error is not None:
                return f"error: {srv._read_error}"
            if srv._proc is not None and srv._proc.returncode is not None:
                return f"exited ({srv._proc.returncode})"
            return f"connected ({len(srv.tools)} tools)"
        when = self._failed.get(name)
        if when is not None:
            remaining = max(0, self.RETRY_AFTER - (time.monotonic() - when))
            return f"failed (retry in {remaining:.0f}s)" if remaining else "failed (retry ready)"
        return "disconnected"

    def call(self, server: str, tool: str, arguments: dict, timeout: float) -> dict:
        srv = self._servers.get(server)
        if srv is None:
            raise McpError(f"server '{server}' is not connected")
        return self._run(srv.call_tool(tool, arguments, timeout), timeout + 10)

    def shutdown(self) -> None:
        with self._server_guard:
            loop = self._loop
            if loop is None:
                return
            for srv in list(self._servers.values()):
                try:
                    asyncio.run_coroutine_threadsafe(srv.stop(), loop).result(timeout=8)
                except Exception:
                    pass
            self._servers.clear()
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=8)
                if not self._thread.is_alive():
                    loop.close()
            self._thread = None
            self._loop = None


MANAGER = Manager()
