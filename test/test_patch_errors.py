"""Patch failures must preserve files and expose actionable errors."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xun.tools.common import get_policy
from xun.tools.fs import fs_write_file
from xun.tools.patch import _run_git_apply, apply_patch
from xun.workspace import DeferredTempDirectory
from test.test_patch import EXPECTED, ORIGINAL, SAMPLE_PATCH, make_ctx


class TestPatchErrors(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ctx = make_ctx(self.root)
        self.file = self.root / "hello.py"
        self.file.write_text(ORIGINAL)

    def test_empty_patch(self):
        for content in ("", " \n\t"):
            with self.subTest(content=content):
                with self.assertRaisesRegex(ValueError, "empty"):
                    apply_patch(self.ctx, content)

    def test_nul_in_patch_is_rejected_before_git(self):
        with patch("xun.tools.patch.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "NUL"):
                apply_patch(self.ctx, SAMPLE_PATCH + "\0")
        run.assert_not_called()
        self.assertEqual(self.file.read_text(), ORIGINAL)

    def test_nul_in_target_beyond_first_chunk_is_rejected(self):
        original = b"prefix\n" + ORIGINAL.encode() + b"x" * 9000 + b"\0\n"
        self.file.write_bytes(original)
        content = SAMPLE_PATCH.replace("@@ -1,3 +1,4 @@", "@@ -2,3 +2,4 @@")
        with self.assertRaisesRegex(ValueError, "target contains NUL"):
            apply_patch(self.ctx, content)
        self.assertEqual(self.file.read_bytes(), original)

    def test_preflight_and_apply_use_only_two_git_calls(self):
        with patch("xun.tools.patch._run_git_apply", wraps=_run_git_apply) as run:
            apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[0].args[2:],
            ("--numstat", "-z", "--summary", "--check"),
        )
        self.assertEqual(run.call_args_list[1].args[2:], ())
        self.assertEqual(self.file.read_text(), EXPECTED)

    def test_invalid_formats_are_rejected_by_git(self):
        for content in (
            "just text",
            "--- header only\n",
            "--- a/hello.py\n+++ b/hello.py\n@@ invalid @@\n",
            "*** Begin Patch\n*** Update File: hello.py\n"
            "@@\n-old\n+new\n*** End Patch\n",
        ):
            with self.subTest(content=content):
                with self.assertRaisesRegex(RuntimeError, "git apply failed"):
                    apply_patch(self.ctx, content)
                self.assertEqual(self.file.read_text(), ORIGINAL)

    def test_error_includes_git_output_and_format_advice(self):
        content = SAMPLE_PATCH.replace('    print("Hi")', '    print("Missing")')
        with self.assertRaises(RuntimeError) as caught:
            apply_patch(self.ctx, content)
        message = str(caught.exception)
        self.assertIn("hello.py", message)
        self.assertIn("patch does not apply", message)
        self.assertIn("Read the current files", message)
        self.assertIn("diff --git a/path b/path", message)

    def test_plain_paths_do_not_get_strip_fallback(self):
        content = SAMPLE_PATCH.replace("a/hello.py", "hello.py").replace(
            "b/hello.py", "hello.py"
        )
        with self.assertRaises(RuntimeError):
            apply_patch(self.ctx, content)
        self.assertEqual(self.file.read_text(), ORIGINAL)

    def test_missing_directory(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            apply_patch(self.ctx, SAMPLE_PATCH, directory="gone")

    def test_directory_must_be_a_directory(self):
        with self.assertRaisesRegex(ValueError, "not a directory"):
            apply_patch(self.ctx, SAMPLE_PATCH, directory="hello.py")

    def test_patch_directory_must_be_inside_workspace(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaisesRegex(ValueError, "workspace"):
                apply_patch(self.ctx, SAMPLE_PATCH, directory=other)

    def test_path_traversal_is_rejected(self):
        for name in ("../escaped.txt", "../../escaped.txt"):
            with self.subTest(name=name):
                content = (
                    f"diff --git a/{name} b/{name}\n"
                    "new file mode 100644\n"
                    f"--- /dev/null\n+++ b/{name}\n@@ -0,0 +1 @@\n+x\n"
                )
                with self.assertRaisesRegex(RuntimeError, "invalid path"):
                    apply_patch(self.ctx, content)

    def test_symlink_directory_cannot_escape_patch_directory(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "hello.py"
            outside.write_text(ORIGINAL)
            (self.root / "link").symlink_to(other, target_is_directory=True)
            content = SAMPLE_PATCH.replace("hello.py", "link/hello.py")
            with self.assertRaisesRegex(RuntimeError, "beyond a symbolic link"):
                apply_patch(self.ctx, content)
            self.assertEqual(outside.read_text(), ORIGINAL)

    def test_absolute_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "hello.py"
            outside.write_text(ORIGINAL)
            content = SAMPLE_PATCH.replace(
                "hello.py", outside.as_posix()
            )
            with self.assertRaisesRegex(RuntimeError, "invalid path"):
                apply_patch(self.ctx, content)
            self.assertEqual(outside.read_text(), ORIGINAL)

    def test_missing_final_newline_requires_marker(self):
        target = self.root / "note.txt"
        target.write_bytes(b"a\nb")
        content = (
            "diff --git a/note.txt b/note.txt\n"
            "--- a/note.txt\n+++ b/note.txt\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
        )
        with self.assertRaisesRegex(RuntimeError, "patch does not apply"):
            apply_patch(self.ctx, content)
        self.assertEqual(target.read_bytes(), b"a\nb")
        marked = content.replace(
            "-b\n+c\n",
            "-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n",
        )
        apply_patch(self.ctx, marked)
        self.assertEqual(target.read_bytes(), b"a\nc")

    def test_missing_git_is_explained(self):
        with patch("xun.tools.patch.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "requires Git"):
                apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertEqual(self.file.read_text(), ORIGINAL)

    def test_timeout_is_not_retried(self):
        error = subprocess.TimeoutExpired("git apply", 60)
        with patch("xun.tools.patch.subprocess.run", side_effect=error) as run:
            with self.assertRaisesRegex(RuntimeError, "timed out.*Inspect"):
                apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(self.file.read_text(), ORIGINAL)

    def test_both_output_streams_are_reported(self):
        failed = subprocess.CompletedProcess(
            ["git"], 1, stdout=b"stdout reason\n", stderr=b"stderr reason\n"
        )
        with patch("xun.tools.patch.subprocess.run", return_value=failed) as run:
            with self.assertRaises(RuntimeError) as caught:
                apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertIn("stdout reason", str(caught.exception))
        self.assertIn("stderr reason", str(caught.exception))
        self.assertEqual(run.call_count, 1)

    def test_successful_git_parse_with_no_changes_is_rejected(self):
        result = subprocess.CompletedProcess(["git"], 0, stdout=b"", stderr=b"")
        with patch("xun.tools.patch.subprocess.run", return_value=result) as run:
            with self.assertRaisesRegex(ValueError, "no file changes"):
                apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertEqual(run.call_count, 1)

    def test_inherited_git_paths_do_not_change_workspace(self):
        with tempfile.TemporaryDirectory() as other:
            other_root = Path(other)
            subprocess.run(["git", "init", "-q", str(other_root)], check=True)
            outside = other_root / "hello.py"
            outside.write_text(ORIGINAL)
            with patch.dict(os.environ, {
                "GIT_DIR": str(other_root / ".git"),
                "GIT_WORK_TREE": str(other_root),
                "GIT_COMMON_DIR": str(other_root / ".git"),
                "GIT_INDEX_FILE": str(other_root / ".git/index"),
            }):
                apply_patch(self.ctx, SAMPLE_PATCH)
            self.assertIn("Welcome!", self.file.read_text())
            self.assertEqual(outside.read_text(), ORIGINAL)

    def test_delete_existing_file_is_rejected(self):
        self.file.write_text("old\n")
        content = (
            "diff --git a/hello.py b/hello.py\ndeleted file mode 100644\n"
            "--- a/hello.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
        )
        with self.assertRaisesRegex(ValueError, "only modifies existing text files"):
            apply_patch(self.ctx, content)
        self.assertEqual(self.file.read_text(), "old\n")

    def test_rename_and_copy_to_existing_destination_are_rejected(self):
        destination = self.root / "other.py"
        destination.write_text("destination\n")
        for operation in ("rename", "copy"):
            with self.subTest(operation=operation):
                content = (
                    "diff --git a/hello.py b/other.py\nsimilarity index 100%\n"
                    f"{operation} from hello.py\n{operation} to other.py\n"
                )
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    apply_patch(self.ctx, content)
                self.assertEqual(self.file.read_text(), ORIGINAL)
                self.assertEqual(destination.read_text(), "destination\n")

    def test_mode_and_type_changes_are_rejected(self):
        before = self.file.stat().st_mode
        for mode in ("100755", "120000"):
            with self.subTest(mode=mode):
                content = (
                    "diff --git a/hello.py b/hello.py\n"
                    f"old mode 100644\nnew mode {mode}\n"
                )
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "only modifies existing text files|does not match old mode",
                ):
                    apply_patch(self.ctx, content)
                self.assertEqual(self.file.stat().st_mode, before)
                self.assertEqual(self.file.read_text(), ORIGINAL)

    def test_mixed_edit_and_delete_does_not_modify_either_file(self):
        other = self.root / "other.py"
        other.write_text("old\n")
        content = SAMPLE_PATCH + (
            "diff --git a/other.py b/other.py\ndeleted file mode 100644\n"
            "--- a/other.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
        )
        with self.assertRaisesRegex(ValueError, "only modifies existing text files"):
            apply_patch(self.ctx, content)
        self.assertEqual(self.file.read_text(), ORIGINAL)
        self.assertEqual(other.read_text(), "old\n")

    def test_binary_patch_is_rejected_before_applying(self):
        target = self.root / "binary.dat"
        target.write_bytes(b"old\0")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "add", "binary.dat"], check=True
        )
        target.write_bytes(b"new\0")
        content = subprocess.run(
            ["git", "-C", str(self.root), "diff", "--binary"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertIn("GIT binary patch", content)
        target.write_bytes(b"old\0")
        with self.assertRaisesRegex(ValueError, "only modifies existing text files"):
            apply_patch(self.ctx, content)
        self.assertEqual(target.read_bytes(), b"old\0")

    def test_symlink_file_is_rejected(self):
        (self.root / "linked.py").symlink_to(self.file)
        content = SAMPLE_PATCH.replace("hello.py", "linked.py")
        with self.assertRaisesRegex(RuntimeError, "patch does not apply"):
            apply_patch(self.ctx, content)
        self.assertEqual(self.file.read_text(), ORIGINAL)


class TestPatchPermissions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ctx = make_ctx(self.root)
        self.policy = get_policy(self.ctx)
        self.policy.write_allowlist.remove(self.root)
        self.file = self.root / "hello.py"
        self.file.write_text(ORIGINAL)

    def test_denied_patch_leaves_file_unchanged(self):
        self.ctx.agent.get_choice.return_value = SimpleNamespace(
            choice="Deny", source="user"
        )
        with self.assertRaisesRegex(RuntimeError, "cancelled by user"):
            apply_patch(self.ctx, SAMPLE_PATCH)
        self.ctx.agent.get_choice.assert_called_once()
        self.assertEqual(self.file.read_text(), ORIGINAL)

    def test_allow_once_does_not_grant_future_writes(self):
        self.ctx.agent.get_choice.return_value = SimpleNamespace(
            choice="Allow once", source="user"
        )
        apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertEqual(self.file.read_text(), EXPECTED)
        self.assertFalse(self.policy.write_allowlist.has(self.file))
        self.ctx.agent.get_choice.assert_called_once()

    def test_user_grant_is_shared_with_fs(self):
        self.ctx.agent.get_choice.return_value = SimpleNamespace(
            choice="Allow, and grant this agent to write to this file/directory.",
            source="user",
        )
        apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertTrue(self.policy.write_allowlist.has(self.file))
        fs_write_file(self.ctx, "hello.py", "written by fs\n")
        self.ctx.agent.get_choice.assert_called_once()
        self.assertEqual(self.file.read_text(), "written by fs\n")

    def test_automatic_choice_does_not_add_persistent_grant(self):
        self.ctx.agent.get_choice.return_value = SimpleNamespace(
            choice="Allow, and grant this agent to write to this file/directory.",
            source="auto",
        )
        apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertFalse(self.policy.write_allowlist.has(self.file))

    def test_allowlisted_file_or_directory_needs_no_prompt(self):
        for target in (self.file, self.root):
            with self.subTest(target=target):
                self.file.write_text(ORIGINAL)
                self.policy.write_allowlist.add(target, is_dir=target.is_dir())
                apply_patch(self.ctx, SAMPLE_PATCH)
                self.assertEqual(self.file.read_text(), EXPECTED)
                self.policy.write_allowlist.remove(target)
        self.ctx.agent.get_choice.assert_not_called()

    def test_temporary_file_needs_no_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.ctx.agent.workspace.tempdir = DeferredTempDirectory(Path(temporary))
            target = Path(temporary) / "hello.py"
            target.write_text(ORIGINAL)
            apply_patch(self.ctx, SAMPLE_PATCH, directory=temporary)
            self.assertEqual(target.read_text(), EXPECTED)
            self.ctx.agent.get_choice.assert_not_called()
            self.assertFalse(self.policy.write_allowlist.has(target))

    def test_carriage_return_filename_cannot_borrow_newline_file_grant(self):
        actual = self.root / "carriage\rname.py"
        other = self.root / "carriage\nname.py"
        actual.write_text(ORIGINAL)
        other.write_text(ORIGINAL)
        self.policy.write_allowlist.add(other)
        self.ctx.agent.get_choice.return_value = SimpleNamespace(
            choice="Deny", source="user"
        )
        content = SAMPLE_PATCH.replace(
            "a/hello.py", '"a/carriage\\rname.py"'
        ).replace("b/hello.py", '"b/carriage\\rname.py"')
        with self.assertRaisesRegex(RuntimeError, "cancelled by user"):
            apply_patch(self.ctx, content)
        self.ctx.agent.get_choice.assert_called_once()
        self.assertIn(str(actual), self.ctx.agent.get_choice.call_args.kwargs["message"])
        self.assertEqual(actual.read_text(), ORIGINAL)
        self.assertEqual(other.read_text(), ORIGINAL)

    def test_denial_for_second_file_leaves_all_files_unchanged(self):
        other = self.root / "other.py"
        other.write_text(ORIGINAL)
        self.ctx.agent.get_choice.side_effect = [
            SimpleNamespace(choice="Allow once", source="user"),
            SimpleNamespace(choice="Deny", source="user"),
        ]
        content = SAMPLE_PATCH + SAMPLE_PATCH.replace("hello.py", "other.py")
        with self.assertRaisesRegex(RuntimeError, "cancelled by user"):
            apply_patch(self.ctx, content)
        self.assertEqual(self.ctx.agent.get_choice.call_count, 2)
        self.assertEqual(self.file.read_text(), ORIGINAL)
        self.assertEqual(other.read_text(), ORIGINAL)

    def test_invalid_patch_does_not_prompt(self):
        content = SAMPLE_PATCH.replace('    print("Hi")', '    print("Missing")')
        with self.assertRaises(RuntimeError):
            apply_patch(self.ctx, content)
        self.ctx.agent.get_choice.assert_not_called()


if __name__ == "__main__":
    unittest.main()
