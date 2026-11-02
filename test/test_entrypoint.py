import os
import unittest
import sys
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from xun.displays import SimpleSession
from xun.entrypoint import main_container, main_serve, web_session
from xun.session import DurableSession


class _Workspace:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir

    def to_json(self) -> dict:
        return {"workdir": str(self.workdir), "tempdir": None}


class _Agent:
    def __init__(self, workdir: Path | None) -> None:
        self.name = "fake"
        self.workspace = _Workspace(workdir if workdir is not None else Path.cwd())
        self.command = Mock()
        self.conversation = Mock()
        self.state = Mock()
        self.state.to_json.return_value = {}
        self.finalized = False
        self.identifier = uuid.uuid4().hex
        self.hooks = Mock()

    def finalize(self) -> None:
        self.finalized = True


class _Service:
    instances: list["_Service"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.mounts = []
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def mount(self, session):
        self.mounts.append(session)
        return self

    def start(self, *, blocking=False):
        self.started = blocking

    def stop(self):
        self.stopped = True


class WebSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        _Service.instances.clear()
        self.agents: list[_Agent] = []
        self.home = TemporaryDirectory()
        patcher = patch.dict(os.environ, {"XUN_HOME": self.home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.home.cleanup)

    def setup_agent(self, **kwargs):
        workdir = kwargs.get("workdir")
        agent = _Agent(Path(workdir) if workdir is not None else None)
        self.agents.append(agent)
        if kwargs.get("pre_init") is not None:
            kwargs["pre_init"](agent)
        return agent

    def test_fixed_workdir_is_shared_by_ephemeral_sessions(self) -> None:
        with TemporaryDirectory() as directory, \
                patch("xun.entrypoint.WebDisplayService", _Service), \
                patch("xun.entrypoint.setup_agent", side_effect=self.setup_agent):
            web_session(workdir=directory, manage_sessions=True)
            service = _Service.instances[0]
            session = service.kwargs["session_factory"]()

        self.assertIsInstance(session, SimpleSession)
        self.assertEqual([agent.workspace.workdir for agent in self.agents], [Path(directory), Path(directory)])
        session.close()
        self.assertTrue(all(agent.finalized for agent in self.agents))
        self.assertTrue(service.started)
        self.assertTrue(service.stopped)

    def test_missing_workdir_sessions_persist_until_destroyed(self) -> None:
        with patch("xun.entrypoint.WebDisplayService", _Service), \
                patch("xun.entrypoint.setup_agent", side_effect=self.setup_agent):
            web_session(manage_sessions=True)
            service = _Service.instances[0]
            initial = service.mounts[0]
            self.assertIsInstance(initial, DurableSession)
            session = service.kwargs["session_factory"]()
            workdir = self.agents[1].workspace.workdir
            self.assertTrue(workdir.is_dir())
            self.assertEqual(workdir.parent, session.directory)
            session.close()
            self.assertTrue(self.agents[1].finalized)
            self.assertTrue(workdir.is_dir())  # close keeps the data
            session.destroy()
            self.assertFalse(session.directory.exists())

        initial_slot = self.agents[0].workspace.workdir
        self.assertTrue(self.agents[0].finalized)  # initial closed by web_session teardown
        self.assertEqual(initial_slot.parent, initial.directory)
        self.assertTrue(initial.directory.exists())  # teardown keeps every session

    def test_stored_sessions_are_remounted_without_a_fresh_one(self) -> None:
        with patch("xun.entrypoint.WebDisplayService", _Service), \
                patch("xun.entrypoint.setup_agent", side_effect=self.setup_agent):
            web_session(manage_sessions=True)
            first = _Service.instances[0]
            restored = first.mounts[0]  # the initial session, closed by teardown

            web_session(manage_sessions=True)
            second = _Service.instances[-1]

        self.assertEqual([s.mount_path for s in second.mounts], [restored.mount_path])  # nothing new on top
        self.assertEqual([agent.workspace.workdir.parent for agent in self.agents[1:]],
                         [restored.directory])
        self.assertEqual(second.mounts[0].name, restored.name)  # name survives via session.json
        self.assertTrue(all(agent.finalized for agent in self.agents))

    def test_fresh_session_when_nothing_is_stored(self) -> None:
        with patch("xun.entrypoint.WebDisplayService", _Service), \
                patch("xun.entrypoint.setup_agent", side_effect=self.setup_agent):
            web_session(manage_sessions=True)
            _Service.instances[0].mounts[0].destroy()  # the user removes the only session

            web_session(manage_sessions=True)
            second = _Service.instances[-1]

        self.assertEqual(len(second.mounts), 1)
        self.assertIsInstance(second.mounts[0], DurableSession)  # a fresh one takes over

    def test_session_management_can_be_disabled(self) -> None:
        with patch("xun.entrypoint.WebDisplayService", _Service), \
                patch("xun.entrypoint.setup_agent", side_effect=self.setup_agent):
            web_session(manage_sessions=False)

        self.assertIsNone(_Service.instances[0].kwargs["session_factory"])

    def test_initial_agent_can_be_disabled(self) -> None:
        with patch("xun.entrypoint.WebDisplayService", _Service), \
                patch("xun.entrypoint.setup_agent", side_effect=self.setup_agent):
            web_session(initial_agent=False)
            service = _Service.instances[0]
            self.assertEqual(self.agents, [])
            session = service.kwargs["session_factory"]()
            session.destroy()

        self.assertEqual(len(self.agents), 1)
        self.assertEqual(service.mounts[0].mount_path, "/")
        self.assertTrue(service.started)
        self.assertTrue(service.stopped)


class EntrypointCliTest(unittest.TestCase):
    def test_xuns_accepts_one_workdir_and_session_management_flag(self) -> None:
        run = Mock()
        with patch.object(sys, "argv", ["xuns", "/tmp/project", "--no-manage-sessions", "--no-initial-agent", "--base-path", "/alpha"]), \
                patch("xun.entrypoint.web_session", run):
            main_serve()

        self.assertEqual(run.call_args.kwargs["workdir"], "/tmp/project")
        self.assertFalse(run.call_args.kwargs["manage_sessions"])
        self.assertFalse(run.call_args.kwargs["initial_agent"])
        self.assertEqual(run.call_args.kwargs["base_path"], "/alpha")

    def test_xuns_rejects_multiple_workdirs(self) -> None:
        with patch.object(sys, "argv", ["xuns", "/tmp/one", "/tmp/two"]), \
                patch("xun.entrypoint.web_session"):
            with self.assertRaises(SystemExit):
                main_serve()

    def test_xunc_without_workdir_uses_container_temporary_workspace(self) -> None:
        client = Mock()
        container = Mock(id="container-id")
        client.containers.create.return_value = container
        with patch.object(sys, "argv", ["xunc"]), \
                patch.dict("os.environ", {"TERM": "xterm-256color"}), \
                patch("xun.entrypoint.docker.from_env", return_value=client), \
                patch("xun.entrypoint.start_attached") as start_attached:
            main_container()

        options = client.containers.create.call_args.kwargs
        self.assertIsNone(options["volumes"])
        self.assertEqual(options["command"], ["xuns", "", "--host", "0.0.0.0"])
        self.assertEqual(options["environment"]["TERM"], "xterm-256color")
        start_attached.assert_called_once_with(container, interactive=True)
        client.close.assert_called_once_with()

    def test_xunc_closes_client_when_container_creation_fails(self) -> None:
        client = Mock()
        client.containers.create.side_effect = RuntimeError("create")
        with patch.object(sys, "argv", ["xunc"]), \
                patch("xun.entrypoint.docker.from_env", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "create"):
                main_container()

        client.close.assert_called_once_with()

    def test_xunc_with_workdir_mounts_workspace(self) -> None:
        client = Mock()
        container = Mock(id="container-id")
        client.containers.create.return_value = container

        with TemporaryDirectory() as directory, \
                patch.object(sys, "argv", ["xunc", directory]), \
                patch("xun.entrypoint.docker.from_env", return_value=client), \
                patch("xun.entrypoint.start_attached"):
            main_container()

        options = client.containers.create.call_args.kwargs
        self.assertEqual(
            options["volumes"],
            {str(Path(directory).resolve()): {"bind": "/workspace", "mode": "rw"}},
        )
        self.assertEqual(options["working_dir"], "/workspace")
        self.assertEqual(options["command"], ["xuns", ".", "--host", "0.0.0.0"])


if __name__ == "__main__":
    unittest.main()
