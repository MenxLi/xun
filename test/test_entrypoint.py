import unittest
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from xun.entrypoint import main_container, main_serve, web_session


class _Agent:
    def __init__(self, workdir: Path) -> None:
        self.workspace = type("Workspace", (), {"workdir": workdir})()
        self.finalized = False

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

    def mount(self, path, display, *, name=None):
        self.mounts.append((path, display, name))
        return self

    def start(self, *, blocking=False):
        self.started = blocking

    def stop(self):
        self.stopped = True


class WebSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        _Service.instances.clear()
        self.agents: list[_Agent] = []

    def setup_agent(self, **kwargs):
        agent = _Agent(Path(kwargs["workdir"]))
        self.agents.append(agent)
        return agent

    def test_fixed_workdir_is_shared_by_managed_sessions(self) -> None:
        with TemporaryDirectory() as directory, \
                patch("xun.entrypoint.WebDisplayService", _Service), \
                patch("xun.entrypoint.setup_agent", side_effect=self.setup_agent):
            web_session(workdir=directory, manage_sessions=True)
            service = _Service.instances[0]
            with service.kwargs["session_manager"]() as (_path, _display):
                pass

        self.assertEqual([agent.workspace.workdir for agent in self.agents], [Path(directory), Path(directory)])
        self.assertTrue(all(agent.finalized for agent in self.agents))
        self.assertTrue(service.started)
        self.assertTrue(service.stopped)

    def test_missing_workdir_creates_distinct_temporary_workspaces(self) -> None:
        with patch("xun.entrypoint.WebDisplayService", _Service), \
                patch("xun.entrypoint.setup_agent", side_effect=self.setup_agent):
            web_session(manage_sessions=True)
            service = _Service.instances[0]
            initial_workdir = self.agents[0].workspace.workdir
            with service.kwargs["session_manager"]() as (_path, _display):
                managed_workdir = self.agents[1].workspace.workdir
                self.assertTrue(managed_workdir.exists())
                self.assertTrue(managed_workdir.name.endswith("-workspace"))
            self.assertFalse(managed_workdir.exists())

        self.assertNotEqual(initial_workdir, managed_workdir)
        self.assertTrue(initial_workdir.name.endswith("-workspace"))
        self.assertFalse(initial_workdir.exists())

    def test_session_management_can_be_disabled(self) -> None:
        with patch("xun.entrypoint.WebDisplayService", _Service), \
                patch("xun.entrypoint.setup_agent", side_effect=self.setup_agent):
            web_session(manage_sessions=False)

        self.assertIsNone(_Service.instances[0].kwargs["session_manager"])


class EntrypointCliTest(unittest.TestCase):
    def test_xuns_accepts_one_workdir_and_session_management_flag(self) -> None:
        run = Mock()
        with patch.object(sys, "argv", ["xuns", "/tmp/project", "--no-manage-sessions", "--base-path", "/alpha"]), \
                patch("xun.entrypoint.web_session", run):
            main_serve()

        self.assertEqual(run.call_args.kwargs["workdir"], "/tmp/project")
        self.assertFalse(run.call_args.kwargs["manage_sessions"])
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
                patch("xun.entrypoint.docker.from_env", return_value=client), \
                patch("xun.entrypoint.start_attached") as start_attached:
            main_container()

        options = client.containers.create.call_args.kwargs
        self.assertIsNone(options["volumes"])
        self.assertEqual(options["command"], ["xuns", "", "--host", "0.0.0.0"])
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
        self.assertEqual(options["command"], ["xuns", "--host", "0.0.0.0"])


if __name__ == "__main__":
    unittest.main()
