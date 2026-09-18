import sqlite3
import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from docker.errors import NotFound

from xun.supervisor.runtime import DockerManager, ManagedContainer
from xun.supervisor.service import Multiplexer, Supervisor
from xun.supervisor.users import User, UserStore


class UserStoreTest(unittest.TestCase):
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

    def test_upgrade_bumps_generation(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "xunx.db")
            alice = store.add("alice")
            self.assertEqual(alice.generation, 0)
            self.assertEqual(store.get("alice"), alice)
            self.assertIsNone(store.get("nobody"))

            upgraded = store.upgrade("alice")
            self.assertIsNotNone(upgraded)
            assert upgraded is not None
            self.assertEqual(upgraded.generation, 1)
            self.assertEqual(store.list(), [upgraded])
            self.assertIsNone(store.upgrade("nobody"))

    def test_migrates_databases_without_generation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "xunx.db"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE users (name TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE)"
                )
                connection.execute("INSERT INTO users VALUES (?, ?)", ("alice", "tok"))

            store = UserStore(path)

            self.assertEqual(store.get("alice"), User("alice", "tok", 0))


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

        self.assertEqual(container, ManagedContainer("container-id", 20001, "user-token", 0))
        options = client.containers.create.call_args.kwargs
        self.assertEqual(options["ports"], {"20001/tcp": ("127.0.0.1", 20001)})
        self.assertEqual(options["labels"]["xunx.token"], "user-token")
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

    def _adopt_manager(self, attrs: dict) -> tuple[DockerManager, Mock]:
        client = Mock()
        sdk_container = client.containers.get.return_value
        sdk_container.id = "container-id"
        sdk_container.attrs = attrs
        return DockerManager(image="xun", port_range=range(0, 0), instance="instance", client=client), client

    def test_adopt_resumes_running_container(self) -> None:
        manager, client = self._adopt_manager({})
        client.containers.get.return_value.attrs = {
            "Id": "container-id",
            "State": {"Status": "running"},
            "Config": {"Labels": {"xunx.gen": "3", "xunx.token": "user-token"}},
            "NetworkSettings": {"Ports": {"21001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "21001"}]}},
        }

        container = manager.adopt(User("alice", "user-token", 3))

        self.assertEqual(container, ManagedContainer("container-id", 21001, "user-token", 3))
        self.assertIn(21001, manager.used_ports)

    def test_adopt_skips_missing_or_stopped_container(self) -> None:
        manager, client = self._adopt_manager({"State": {"Status": "exited"}})
        self.assertIsNone(manager.adopt(User("alice", "token")))

        client.containers.get.side_effect = NotFound("no such container")
        self.assertIsNone(manager.adopt(User("alice", "token")))


class _Docker:
    def __init__(self) -> None:
        self.started: list[User] = []
        self.stopped: list[ManagedContainer] = []
        self.running = True
        self.adopt_result: ManagedContainer | None = None
        self.adopted: list[User] = []
        self.pruned: list[set[str]] = []

    def start(self, user: User) -> ManagedContainer:
        self.started.append(user)
        return ManagedContainer(f"container-{user.name}", 21000 + len(self.started), user.token, user.generation)

    def adopt(self, user: User) -> ManagedContainer | None:
        self.adopted.append(user)
        return self.adopt_result

    def stop(self, container: ManagedContainer) -> None:
        self.stopped.append(container)

    def is_running(self, container: ManagedContainer) -> bool:
        del container
        return self.running

    def prune(self, keep_ids: set[str]) -> None:
        self.pruned.append(keep_ids)

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

    async def test_adopts_live_container_after_supervisor_restart(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "xunx.db")
            alice = store.add("alice")
            docker = _Docker()
            survivor = ManagedContainer("old-container", 21500, alice.token, 0)
            docker.adopt_result = survivor
            supervisor = Supervisor(store, docker, interval=1)

            await supervisor.reconcile()

            self.assertIs(supervisor.active["alice"], survivor)
            self.assertEqual(docker.started, [])
            self.assertEqual(docker.pruned, [{"old-container"}])

    async def test_upgrade_recreates_container_with_stale_generation(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "xunx.db")
            alice = store.add("alice")
            docker = _Docker()
            docker.adopt_result = ManagedContainer("old-container", 21500, alice.token, alice.generation)
            supervisor = Supervisor(store, docker, interval=1)

            store.upgrade("alice")
            await supervisor.reconcile()

            self.assertEqual([c.id for c in docker.stopped], ["old-container"])
            self.assertEqual([u.name for u in docker.started], ["alice"])
            self.assertEqual(supervisor.active["alice"].generation, alice.generation + 1)
            # pruning runs once per supervisor lifetime
            await supervisor.reconcile()
            self.assertEqual(len(docker.pruned), 1)

    async def test_adopted_container_with_rotated_token_is_recreated(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "xunx.db")
            docker = _Docker()
            # user was re-added while the supervisor was down: stale container token
            store.add("alice")
            store.delete("alice")
            alice = store.add("alice")
            docker.adopt_result = ManagedContainer("old-container", 21500, "stale-token", alice.generation)
            supervisor = Supervisor(store, docker, interval=1)

            await supervisor.reconcile()

            self.assertEqual([c.id for c in docker.stopped], ["old-container"])
            self.assertEqual(supervisor.active["alice"].token, alice.token)

    async def test_lifecycle_keeps_containers_on_shutdown(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "xunx.db")
            store.add("alice")
            docker = _Docker()
            supervisor = Supervisor(store, docker, interval=3600)

            async with supervisor.lifecycle():
                self.assertIn("alice", supervisor.active)

            self.assertEqual(docker.stopped, [])
            self.assertEqual(len(supervisor.active), 1)

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

    async def test_lifecycle_closes_backend_when_startup_fails(self) -> None:
        backend = Mock()
        backend.prune.return_value = None
        supervisor = Supervisor(Mock(), backend, interval=1)

        async def failing_reconcile() -> None:
            raise RuntimeError("reconcile")

        supervisor.reconcile = failing_reconcile
        with self.assertRaisesRegex(RuntimeError, "reconcile"):
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
        proxy_app = Multiplexer(
            supervisor,
            manage_supervisor=False,
            websocket_heartbeat=0.05,
        ).app()
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

    async def test_sends_websocket_heartbeat_to_downstream_client(self) -> None:
        websocket = await self.client.ws_connect("/alice/socket", autoping=False)

        message = await websocket.receive(timeout=0.5)

        self.assertEqual(message.type, WSMsgType.PING)
        await websocket.close()

    async def test_websocket_remains_usable_after_heartbeats(self) -> None:
        websocket = await self.client.ws_connect("/alice/socket", compress=15)
        response = asyncio.create_task(websocket.receive(timeout=0.5))

        await asyncio.sleep(0.15)
        await websocket.send_str("after-idle")
        message = await response

        self.assertEqual(message.data, "/alice/socket:after-idle")
        await websocket.close()

if __name__ == "__main__":
    unittest.main()