import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from xun.supervisor.runtime import copy_directory, start_attached


class ContainerRuntimeTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()