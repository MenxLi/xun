import asyncio
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock, patch

from extensions.mcp.client import MAX_MESSAGE_SIZE, Manager, McpError, StdioServer
from extensions.mcp.setup_extension import setup_extension


SERVER = '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "tools/list":
        result = {"tools": [{"name": "screenshot"}]}
    elif method == "tools/call":
        result = {"content": [{"type": "image", "data": "A" * 100000,
                               "mimeType": "image/png"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                      "result": result}), flush=True)
'''


class StdioTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_broken_pipe_marks_connection_unusable(self) -> None:
        server = StdioServer("image", "unused")
        writer = Mock()
        writer.drain = Mock(side_effect=BrokenPipeError("stdin closed"))
        server._proc = Mock(stdin=writer, returncode=None)
        other_request = asyncio.get_running_loop().create_future()
        server._pending[42] = other_request

        with self.assertRaisesRegex(McpError, "write failed"):
            await server.call_tool("screenshot", {}, 1)
        with self.assertRaisesRegex(McpError, "write failed"):
            await other_request

        manager = Manager()
        manager._servers["image"] = server
        self.assertIn("write failed", manager.status("image"))
        with self.assertRaisesRegex(McpError, "write failed"):
            await server.call_tool("screenshot", {}, 1)

    async def test_large_image_response(self) -> None:
        server = StdioServer("image", sys.executable, ["-u", "-c", SERVER])
        try:
            await server.start()
            result = await server.call_tool("screenshot", {}, 2)
            self.assertEqual(len(result["content"][0]["data"]), 100000)
            self.assertEqual(MAX_MESSAGE_SIZE, 16 * 1024 * 1024)
        finally:
            await server.stop()

    async def test_read_failure_rejects_pending_and_future_calls(self) -> None:
        server = StdioServer("broken", "unused")
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"x" * 128)
        server._proc = Mock(stdout=reader, returncode=None)
        pending = asyncio.get_running_loop().create_future()
        server._pending[1] = pending

        await server._read_loop()

        with self.assertRaisesRegex(McpError, "chunk exceed the limit"):
            await pending
        with self.assertRaisesRegex(McpError, "read failed"):
            await server.call_tool("screenshot", {}, 1)
        self.assertFalse(server._pending)


class ManagerRecoveryTest(unittest.TestCase):
    def test_concurrent_ensure_starts_only_one_process(self) -> None:
        manager = Manager()
        config = {"command": sys.executable, "args": ["-u", "-c", SERVER]}
        barrier = Barrier(3)

        def ensure() -> list[dict]:
            barrier.wait()
            return manager.ensure("image", config)

        try:
            with patch("extensions.mcp.client.StdioServer", wraps=StdioServer) as spawn:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(ensure) for _ in range(2)]
                    barrier.wait()
                    self.assertEqual(futures[0].result(), futures[1].result())
                self.assertEqual(spawn.call_count, 1)
        finally:
            manager.shutdown()

    def test_status_does_not_start_or_contact_server(self) -> None:
        manager = Manager()
        self.assertEqual(manager.status("image"), "disconnected")
        self.assertIsNone(manager._loop)
        server = Mock(tools=[{}], _read_error=None, _proc=Mock(returncode=None))
        manager._servers["image"] = server
        self.assertEqual(manager.status("image"), "connected (1 tools)")
        server._proc.returncode = 1
        self.assertEqual(manager.status("image"), "exited (1)")
        server._read_error = McpError("read failed")
        self.assertEqual(manager.status("image"), "error: read failed")
        manager._servers.clear()
        manager._failed["image"] = 0
        self.assertEqual(manager.status("image"), "failed (retry ready)")
        self.assertIsNone(manager._loop)

    def test_dead_process_reconnects_and_restart_replaces_live_process(self) -> None:
        manager = Manager()
        config = {"command": sys.executable, "args": ["-u", "-c", SERVER]}
        try:
            self.assertEqual(manager.ensure("image", config), [{"name": "screenshot"}])
            first = manager._servers["image"]
            manager._run(first.stop(), 8)

            self.assertEqual(manager.ensure("image", config), [{"name": "screenshot"}])
            second = manager._servers["image"]
            self.assertIsNot(second, first)
            self.assertEqual(len(manager.call("image", "screenshot", {}, 2)
                                 ["content"][0]["data"]), 100000)

            self.assertEqual(manager.restart("image", config), [{"name": "screenshot"}])
            self.assertIsNot(manager._servers["image"], second)
            self.assertIsNotNone(second._proc.returncode)
        finally:
            manager.shutdown()

    def test_failed_start_is_cleaned_up_and_restart_bypasses_backoff(self) -> None:
        manager = Manager()
        config = {"command": sys.executable, "args": ["-u", "-c", "import sys; sys.exit(1)"]}
        try:
            with self.assertRaises(McpError):
                manager.ensure("broken", config)
            self.assertNotIn("broken", manager._servers)
            with self.assertRaisesRegex(McpError, "backoff"):
                manager.ensure("broken", config)
            with self.assertRaises(McpError) as failure:
                manager.restart("broken", config)
            self.assertNotIn("backoff", str(failure.exception))
        finally:
            manager.shutdown()


class McpCommandTest(unittest.TestCase):
    def test_status_and_restart_available_after_failed_start(self) -> None:
        from xun import Agent, NullDisplay

        agent = Agent(display=NullDisplay())
        agent.config.extension_settings = {"mcp": {"servers": {
            "image": {"command": "unused"}, "other": {"command": "unused"}}}}
        context = Mock(agent=agent)
        with patch("extensions.mcp.setup_extension.MANAGER") as manager, \
             patch.object(agent, "info") as info, patch.object(agent, "error") as error:
            manager.ensure.side_effect = McpError("start failed")
            setup_extension(context)
            names = [tool.name for tool in agent.toolbox.list_tools()]
            self.assertNotIn("mcp_restart", names)
            self.assertIn("mcp_refresh", names)
            command = agent.command.get("mcp")
            self.assertIsNotNone(command)

            manager.status.side_effect = {"image": "failed", "other": "connected"}.get
            command.invoke(agent, ["status"])
            info.assert_called_with("image: failed\nother: connected")
            command.invoke(agent, ["status", "image"])
            info.assert_called_with("image: failed")
            manager.restart.assert_not_called()

            manager.restart.return_value = [{"name": "screenshot"}]
            command.invoke(agent, ["restart", "image"])
            manager.restart.assert_called_once_with("image", {"command": "unused"})
            info.assert_called_with("image: restarted (added 1, replaced 0, removed 0)")
            self.assertIn("mcp_image__screenshot",
                          [tool.name for tool in agent.toolbox.list_tools()])

            command.invoke(agent, ["restart", "missing"])
            error.assert_called_once()
            manager.restart.assert_called_once()