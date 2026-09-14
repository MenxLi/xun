import sqlite3
import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from xun.supervisor.runtime import DockerManager, ManagedContainer
from xun.supervisor.service import Multiplexer, Supervisor
from xun.supervisor.users import User, UserStore


class UserStoreTest(unittest.TestCase):
    def test_closes_database_connections(self) -> None:
        store = UserStore.__new__(UserStore)
        store.path = Path("xunx.db")
        connection = MagicMock()

        with patch("xun.supervisor.users.sqlite3.connect", return_value=connection):
            with store._connect() as opened:
                self.assertIs(opened, connection)

        connection.close.assert_called_once_with()

    def test_add_list_and_delete_users(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "xunx.db")
            bob = store.add("bob")
            alice = store.add("alice")

            self.assertEqual(store.list(), [alice, bob])
            self.assertEqual(alice.base_path, "/alice")
            self.assertGreaterEqual(len(alice.token), 24)
            self.assertTrue(store.delete("alice"))
            self.assertFalse(store.delete("alice"))
            self.assertEqual(store.list(), [bob])

    def test_rejects_invalid_and_duplicate_usernames(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "xunx.db")
            store.add("valid_user-1")

            with self.assertRaisesRegex(ValueError, "already exists"):
                store.add("valid_user-1")
            with self.assertRaisesRegex(ValueError, "may only contain"):
                store.add("invalid/user")

    def test_enables_wal_mode(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "xunx.db"
            UserStore(path)

            with sqlite3.connect(path) as connection:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

            self.assertEqual(mode, "wal")


class DockerManagerTest(unittest.TestCase):
    def test_starts_isolated_container_with_expected_xuns_command(self) -> None:
        client = Mock()
        sdk_container = Mock(id="container-id")
        client.containers.create.return_value = sdk_container
        manager = DockerManager(
            image="custom-xun",
            port_range=range(20000, 20002),
            instance="instance-id",
            env_patterns=["XUN_*", "_XUN_*"],
            client=client,
        )
        with patch.dict("os.environ", {"XUN_OPENAI_API_KEY": "secret", "XUN_HOME": "/host"}, clear=True), \
                patch("xun.supervisor.runtime.secrets.choice", return_value=20001):
            container = manager.start(User("alice", "user-token"))

        self.assertEqual(container, ManagedContainer("container-id", 20001, "user-token"))
        options = client.containers.create.call_args.kwargs
        self.assertEqual(options["ports"], {"20001/tcp": ("127.0.0.1", 20001)})
        self.assertEqual(options["environment"]["XUN_OPENAI_API_KEY"], "secret")
        self.assertNotIn("XUN_HOME", options["environment"])
        self.assertEqual(
            options["command"],
            [
                "xuns", "", "--host", "0.0.0.0",
                "--port", "20001", "--token", "user-token",
                "--base-path", "/alice",
            ],
        )
        sdk_container.start.assert_called_once_with()

    def test_removes_created_container_when_start_fails(self) -> None:
        client = Mock()
        sdk_container = Mock(id="container-id")
        sdk_container.start.side_effect = RuntimeError("start")
        client.containers.create.return_value = sdk_container
        manager = DockerManager(
            image="xun",
            port_range=range(20000, 20001),
            instance="instance-id",
            client=client,
        )
        with patch("xun.supervisor.runtime.secrets.choice", return_value=20000), \
                self.assertRaises(RuntimeError):
            manager.start(User("alice", "token"))

        sdk_container.remove.assert_called_once_with(force=True)
        self.assertNotIn(20000, manager.used_ports)

    def test_does_not_remove_container_when_create_fails(self) -> None:
        client = Mock()
        client.containers.create.side_effect = RuntimeError("create")
        manager = DockerManager(
            image="xun",
            port_range=range(20000, 20001),
            instance="instance-id",
            client=client,
        )

        with patch("xun.supervisor.runtime.secrets.choice", return_value=20000), \
                self.assertRaises(RuntimeError):
            manager.start(User("alice", "token"))

        client.containers.get.assert_not_called()
        self.assertNotIn(20000, manager.used_ports)


class _Docker:
    def __init__(self) -> None:
        self.started: list[User] = []
        self.stopped: list[ManagedContainer] = []
        self.running = True

    def start(self, user: User) -> ManagedContainer:
        self.started.append(user)
        return ManagedContainer(f"container-{user.name}", 21000 + len(self.started), user.token)

    def stop(self, container: ManagedContainer) -> None:
        self.stopped.append(container)

    def is_running(self, container: ManagedContainer) -> bool:
        del container
        return self.running

    def cleanup(self) -> None:
        pass

    def close(self) -> None:
        pass


class SupervisorTest(unittest.IsolatedAsyncioTestCase):
    async def test_reconciles_added_deleted_and_crashed_users(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "xunx.db")
            alice = store.add("alice")
            docker = _Docker()
            supervisor = Supervisor(store, docker, interval=1)

            await supervisor.reconcile()
            first = supervisor.active["alice"]
            self.assertEqual(docker.started, [alice])

            store.delete("alice")
            replacement = store.add("alice")
            await supervisor.reconcile()
            self.assertEqual(docker.stopped, [first])
            self.assertEqual(docker.started, [alice, replacement])

            docker.running = False
            await supervisor.reconcile()
            self.assertEqual(len(docker.stopped), 2)
            self.assertEqual(docker.started, [alice, replacement, replacement])

            docker.running = True
            store.delete("alice")
            await supervisor.reconcile()
            self.assertNotIn("alice", supervisor.active)

            bob = store.add("bob")
            await supervisor.reconcile()
            bob_container = supervisor.active["bob"]
            await supervisor.stop()
            self.assertIn(bob, docker.started)
            self.assertIn(bob_container, docker.stopped)
            self.assertEqual(supervisor.active, {})

    async def test_run_continues_after_reconcile_failure(self) -> None:
        supervisor = Supervisor(Mock(), Mock(), interval=0)
        reconciled = 0

        async def reconcile() -> None:
            nonlocal reconciled
            reconciled += 1
            if reconciled == 1:
                raise RuntimeError("temporary")
            raise asyncio.CancelledError

        supervisor.reconcile = reconcile
        with patch("sys.stderr"):
            with self.assertRaises(asyncio.CancelledError):
                await supervisor.run()

        self.assertEqual(reconciled, 2)

    async def test_stop_attempts_every_container(self) -> None:
        docker = Mock()
        docker.stop.side_effect = [RuntimeError("first"), None]
        supervisor = Supervisor(Mock(), docker, interval=1)
        first = ManagedContainer("first", 21001, "token")
        second = ManagedContainer("second", 21002, "token")
        supervisor.active = {"first": first, "second": second}

        with patch("sys.stderr"):
            await supervisor.stop()

        self.assertEqual([call.args[0] for call in docker.stop.call_args_list], [first, second])
        self.assertEqual(supervisor.active, {})

    async def test_lifecycle_closes_backend_when_startup_fails(self) -> None:
        backend = Mock()
        backend.cleanup.side_effect = RuntimeError("cleanup")
        supervisor = Supervisor(Mock(), backend, interval=1)

        with self.assertRaisesRegex(RuntimeError, "cleanup"):
            async with supervisor.lifecycle():
                self.fail("lifecycle should not start")

        backend.close.assert_called_once_with()


class MultiplexerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        async def upstream(request: web.Request) -> web.StreamResponse:
            if request.headers.get("Upgrade", "").lower() == "websocket":
                websocket = web.WebSocketResponse()
                await websocket.prepare(request)
                async for message in websocket:
                    if message.type == WSMsgType.TEXT:
                        await websocket.send_str(f"{request.path}:{message.data}")
                return websocket
            return web.Response(text=request.path_qs)

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{path:.*}", upstream)
        self.upstream = TestServer(upstream_app)
        await self.upstream.start_server()
        upstream_port = self.upstream.port
        assert isinstance(upstream_port, int)

        supervisor = Mock()
        supervisor.active = {"alice": ManagedContainer("container", upstream_port, "token")}
        proxy_app = Multiplexer(supervisor, manage_supervisor=False).app()
        self.client = TestClient(TestServer(proxy_app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.upstream.close()

    async def test_proxies_http_without_removing_base_path(self) -> None:
        response = await self.client.get("/alice/api/value?x=1")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), "/alice/api/value?x=1")

        missing = await self.client.get("/bob")
        self.assertEqual(missing.status, 404)

    async def test_proxies_websocket_without_removing_base_path(self) -> None:
        websocket = await self.client.ws_connect("/alice/socket")
        await websocket.send_str("hello")
        message = await websocket.receive()

        self.assertEqual(message.data, "/alice/socket:hello")
        await websocket.close()


if __name__ == "__main__":
    unittest.main()