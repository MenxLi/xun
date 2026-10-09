import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from xun.supervisor import runtime
from xun.supervisor.runtime import copy_directory, container_name, start_attached, start_shell


class ContainerRuntimeTest(unittest.TestCase):
    def test_interactive_start_uses_docker_cli_with_inherited_terminal(self) -> None:
        container = Mock(id="container-id")
        with patch.object(runtime.subprocess, "run") as run:
            start_attached(container, interactive=True)

        run.assert_called_once_with(["docker", "start", "-ai", "container-id"], check=True)
        container.attach_socket.assert_not_called()
        container.start.assert_not_called()

    def test_start_attached_closes_stream_when_start_fails(self) -> None:
        container = Mock()
        stream = container.attach_socket.return_value
        container.start.side_effect = RuntimeError("start")

        with self.assertRaisesRegex(RuntimeError, "start"):
            start_attached(container, interactive=False)

        stream.close.assert_called_once_with()

    def test_copy_directory_uses_tar_archive(self) -> None:
        container = Mock()
        archived_names: list[str] = []

        def put_archive(target, archive) -> None:
            self.assertEqual(target, "/workspace")
            with tarfile.open(fileobj=archive, mode="r") as contents:
                archived_names.extend(contents.getnames())

        container.put_archive.side_effect = put_archive

        with TemporaryDirectory() as directory:
            Path(directory, "hello.txt").write_text("hello", encoding="utf-8")
            copy_directory(directory, container, "/workspace")

        self.assertEqual(archived_names, ["hello.txt"])


class StartShellTest(unittest.TestCase):
    def _shell(self, status: str = "running", **kwargs: object):
        def docker(*args: str) -> str:
            if args[0] == "inspect":
                return status
            raise AssertionError(f"unexpected docker call: {args}")
        with patch.object(runtime, "_docker", side_effect=docker), \
                patch.object(runtime.subprocess, "call", return_value=0) as call:
            code = start_shell("instance", "alice", **kwargs)
        return code, call

    def test_shell_runs_bash_in_container_workdir(self) -> None:
        code, call = self._shell()
        self.assertEqual(code, 0)
        self.assertEqual(
            call.call_args.args[0],
            ["docker", "exec", "-it", container_name("instance", "alice"), "bash"],
        )

    def test_shell_runs_exec_command_with_cwd(self) -> None:
        _, call = self._shell(
            exec_cmd="xune stat budget set --username alice --day 5M", cwd="/workspace"
        )
        self.assertEqual(
            call.call_args.args[0],
            [
                "docker", "exec", "-it", "--workdir", "/workspace",
                container_name("instance", "alice"),
                "xune", "stat", "budget", "set", "--username", "alice", "--day", "5M",
            ],
        )

    def test_paused_container_is_refused(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "paused"):
            self._shell(status="paused")


if __name__ == "__main__":
    unittest.main()