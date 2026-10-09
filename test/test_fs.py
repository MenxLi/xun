"""Read-file output preserves the bytes needed for reliable text patches."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xun.tools.fs import fs_read_file
from xun.tools.patch import apply_patch
from test.test_patch import make_ctx


class TestReadFile(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ctx = make_ctx(self.root)
        self.file = self.root / "note.txt"

    def test_original_line_endings_and_final_newline_are_preserved(self):
        for content in (b"", b"a", b"a\n", b"a\nb", b"a\r\nb\r\n", b"a\rb\r"):
            with self.subTest(content=content):
                self.file.write_bytes(content)
                self.assertEqual(fs_read_file(self.ctx, "note.txt"), content.decode())

    def test_range_preserves_crlf_and_missing_final_newline(self):
        self.file.write_bytes(b"a\r\nb\r\nlast")
        self.assertEqual(fs_read_file(self.ctx, "note.txt", 1, 1), "b\r\n")
        self.assertEqual(fs_read_file(self.ctx, "note.txt", 2), "last")
        self.assertEqual(fs_read_file(self.ctx, "note.txt", 1), "b\r\nlast")

    def test_limited_read_does_not_consume_remaining_lines(self):
        def lines():
            yield "skipped\n"
            yield "returned\r\n"
            raise AssertionError("Read beyond requested range")

        with patch.object(Path, "open") as open_file:
            open_file.return_value.__enter__.return_value = lines()
            self.assertEqual(fs_read_file(self.ctx, "note.txt", 1, 1), "returned\r\n")

    def test_line_numbers_preserve_original_endings(self):
        self.file.write_bytes(b"a\r\nb\r\nlast")
        self.assertEqual(
            fs_read_file(self.ctx, "note.txt", include_line_numbers=True),
            "1: a\r\n2: b\r\n3: last",
        )
        self.assertEqual(
            fs_read_file(self.ctx, "note.txt", 1, 1, True), "2: b\r\n"
        )

    def test_empty_ranges(self):
        self.file.write_bytes(b"a\nb")
        self.assertEqual(fs_read_file(self.ctx, "note.txt", line_limit=0), "")
        self.assertEqual(fs_read_file(self.ctx, "note.txt", line_offset=2), "")
        self.assertEqual(fs_read_file(self.ctx, "note.txt", line_offset=100), "")

    def test_negative_ranges_raise(self):
        self.file.write_bytes(b"a\n")
        for offset, limit in ((-1, None), (0, -1)):
            with self.subTest(offset=offset, limit=limit):
                with self.assertRaisesRegex(ValueError, "non-negative"):
                    fs_read_file(
                        self.ctx, "note.txt", line_offset=offset, line_limit=limit
                    )

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            fs_read_file(self.ctx, "missing.txt")

    def test_read_then_patch_crlf_file(self):
        self.file.write_bytes(b"first\r\nold\r\nlast\r\n")
        self.assertEqual(
            fs_read_file(self.ctx, "note.txt"), "first\r\nold\r\nlast\r\n"
        )
        content = (
            "diff --git a/note.txt b/note.txt\n"
            "--- a/note.txt\n+++ b/note.txt\n@@ -1,3 +1,3 @@\n"
            " first\r\n-old\r\n+new\r\n last\r\n"
        )
        apply_patch(self.ctx, content)
        self.assertEqual(self.file.read_bytes(), b"first\r\nnew\r\nlast\r\n")


if __name__ == "__main__":
    unittest.main()
