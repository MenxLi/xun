"""End-to-end patch tests against real Git and files."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from xun.tools.common import Policy
from xun.tools.patch import apply_patch
from xun.toolbox import ToolBox
from xun.toolcall import ToolCallContext
from xun.workspace import Workspace

SAMPLE_PATCH = """\
diff --git a/hello.py b/hello.py
--- a/hello.py
+++ b/hello.py
@@ -1,3 +1,4 @@
 def greet(name):
-    print("Hi")
+    print(f"Hello, {name}!")
+    print("Welcome!")
     return True
"""

ORIGINAL = 'def greet(name):\n    print("Hi")\n    return True\n'
EXPECTED = 'def greet(name):\n    print(f"Hello, {name}!")\n    print("Welcome!")\n    return True\n'


def make_ctx(workdir: Path) -> MagicMock:
    ctx = MagicMock(spec=ToolCallContext)
    ctx.agent.workspace = Workspace(workdir=workdir)
    policy = Policy()
    policy.write_allowlist.add(workdir, is_dir=True)
    ctx.agent.state.get_entry.return_value = policy
    return ctx


class TestApplyPatch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ctx = make_ctx(self.root)
        self.file = self.root / "hello.py"
        self.file.write_text(ORIGINAL)

    def test_apply_outside_repository(self):
        self.assertFalse((self.root / ".git").exists())
        result = apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertEqual(result, "Applied successfully to 1 file(s): hello.py")
        self.assertEqual(self.file.read_text(), EXPECTED)
        self.assertFalse((self.root / ".git").exists())

    def test_apply_inside_repository_preserves_index(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "add", "hello.py"], check=True
        )
        index = self.root / ".git" / "index"
        before = index.read_bytes()
        apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertEqual(self.file.read_text(), EXPECTED)
        self.assertEqual(index.read_bytes(), before)

    def test_directory_inside_repository_uses_local_paths(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        sub = self.root / "sub"
        sub.mkdir()
        target = sub / "hello.py"
        target.write_text(ORIGINAL)
        apply_patch(self.ctx, SAMPLE_PATCH, directory="sub")
        self.assertEqual(target.read_text(), EXPECTED)
        self.assertEqual(self.file.read_text(), ORIGINAL)

    def test_workspace_inside_repository_uses_local_paths(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        sub = self.root / "sub"
        sub.mkdir()
        target = sub / "hello.py"
        target.write_text(ORIGINAL)
        apply_patch(make_ctx(sub), SAMPLE_PATCH)
        self.assertEqual(target.read_text(), EXPECTED)
        self.assertEqual(self.file.read_text(), ORIGINAL)

    def test_incorrect_hunk_counts_are_recounted(self):
        patch = SAMPLE_PATCH.replace("@@ -1,3 +1,4 @@", "@@ -1,99 +1,99 @@")
        apply_patch(self.ctx, patch)
        self.assertEqual(self.file.read_text(), EXPECTED)

    def test_multi_file_counts_are_recounted(self):
        (self.root / "other.py").write_text(ORIGINAL)
        patch = SAMPLE_PATCH.replace("@@ -1,3 +1,4 @@", "@@ -1,99 +1,99 @@")
        patch += patch.replace("hello.py", "other.py")
        result = apply_patch(self.ctx, patch)
        self.assertEqual(self.file.read_text(), EXPECTED)
        self.assertEqual((self.root / "other.py").read_text(), EXPECTED)
        self.assertIn("2 file(s): hello.py, other.py", result)

    def test_patch_without_final_newline(self):
        apply_patch(self.ctx, SAMPLE_PATCH.rstrip("\n"))
        self.assertEqual(self.file.read_text(), EXPECTED)

    def test_failing_hunk_leaves_file_untouched(self):
        patch = SAMPLE_PATCH + "@@ -10 +11 @@\n-nope\n+yep\n"
        with self.assertRaisesRegex(RuntimeError, "git apply failed"):
            apply_patch(self.ctx, patch)
        self.assertEqual(self.file.read_text(), ORIGINAL)
        self.assertEqual(list(self.root.glob("*.rej")), [])
        self.assertEqual(list(self.root.glob("*.orig")), [])

    def test_failing_file_leaves_all_files_untouched(self):
        patch = SAMPLE_PATCH + SAMPLE_PATCH.replace("hello.py", "missing.py")
        with self.assertRaisesRegex(RuntimeError, "missing.py"):
            apply_patch(self.ctx, patch)
        self.assertEqual(self.file.read_text(), ORIGINAL)
        self.assertFalse((self.root / "missing.py").exists())

    def test_already_applied_patch_is_not_reversed(self):
        apply_patch(self.ctx, SAMPLE_PATCH)
        with self.assertRaisesRegex(RuntimeError, "patch does not apply"):
            apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertEqual(self.file.read_text(), EXPECTED)

    def test_matching_interior_context_can_move(self):
        target = self.root / "note.txt"
        target.write_text("prefix\nextra\na\nold\nb\nsuffix\n")
        patch = (
            "diff --git a/note.txt b/note.txt\n"
            "--- a/note.txt\n+++ b/note.txt\n@@ -2,3 +2,3 @@\n"
            " a\n-old\n+new\n b\n"
        )
        apply_patch(self.ctx, patch)
        self.assertEqual(target.read_text(), "prefix\nextra\na\nnew\nb\nsuffix\n")

    def test_moved_whole_file_patch_requires_updated_header(self):
        self.file.write_text("import sys\nimport os\n\n\n" + ORIGINAL)
        with self.assertRaises(RuntimeError):
            apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertEqual(self.file.read_text(), "import sys\nimport os\n\n\n" + ORIGINAL)
        apply_patch(
            self.ctx,
            SAMPLE_PATCH.replace("@@ -1,3 +1,4 @@", "@@ -5,3 +5,4 @@"),
        )
        self.assertEqual(self.file.read_text(), "import sys\nimport os\n\n\n" + EXPECTED)

    def test_mismatching_context_is_not_fuzzed(self):
        changed = ORIGINAL.replace("return True", "return False")
        self.file.write_text(changed)
        with self.assertRaisesRegex(RuntimeError, "patch does not apply"):
            apply_patch(self.ctx, SAMPLE_PATCH)
        self.assertEqual(self.file.read_text(), changed)

    def test_reject_create_nested_file(self):
        patch = (
            "diff --git a/nested/new.py b/nested/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n+++ b/nested/new.py\n"
            "@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"
        )
        with self.assertRaisesRegex(ValueError, "only modifies existing text files"):
            apply_patch(self.ctx, patch)
        self.assertFalse((self.root / "nested").exists())

    def test_reject_create_and_delete_in_one_patch(self):
        (self.root / "old.txt").write_text("old\n")
        patch = (
            "diff --git a/new.txt b/new.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n"
            "diff --git a/old.txt b/old.txt\n"
            "deleted file mode 100644\n"
            "--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
        )
        with self.assertRaises(ValueError):
            apply_patch(self.ctx, patch)
        self.assertEqual((self.root / "old.txt").read_text(), "old\n")
        self.assertFalse((self.root / "new.txt").exists())

    def test_reject_create_empty_file(self):
        patch = (
            "diff --git a/empty.txt b/empty.txt\n"
            "new file mode 100644\nindex 0000000..e69de29\n"
        )
        with self.assertRaisesRegex(ValueError, "only modifies existing text files"):
            apply_patch(self.ctx, patch)
        self.assertFalse((self.root / "empty.txt").exists())

    def test_reject_rename_only_diff(self):
        patch = (
            "diff --git a/hello.py b/renamed.py\nsimilarity index 100%\n"
            "rename from hello.py\nrename to renamed.py\n"
        )
        with self.assertRaises(ValueError):
            apply_patch(self.ctx, patch)
        self.assertEqual(self.file.read_text(), ORIGINAL)
        self.assertFalse((self.root / "renamed.py").exists())

    def test_git_quoted_unicode_name(self):
        name = "caf\u00e9.txt"
        (self.root / name).write_text("cafe\n")
        patch = (
            'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n'
            '--- "a/caf\\303\\251.txt"\n+++ "b/caf\\303\\251.txt"\n'
            "@@ -1 +1,2 @@\n cafe\n+au lait\n"
        )
        result = apply_patch(self.ctx, patch)
        self.assertIn(name, result)
        self.assertEqual((self.root / name).read_text(), "cafe\nau lait\n")

    def test_space_and_tab_in_filename(self):
        for name in ("space name.txt", "tab\tname.txt"):
            with self.subTest(name=name):
                target = self.root / name
                target.write_text(ORIGINAL)
                quoted = name.replace("\t", "\\t")
                patch = SAMPLE_PATCH.replace(
                    "a/hello.py", f'"a/{quoted}"'
                ).replace("b/hello.py", f'"b/{quoted}"')
                result = apply_patch(self.ctx, patch)
                self.assertIn(name, result)
                self.assertEqual(target.read_text(), EXPECTED)

    def test_tool_schema_and_dispatch(self):
        toolbox = ToolBox().with_defaults("patch", "diagnostic")
        tools = {tool.name: tool for tool in toolbox.list_tools()}
        self.assertNotIn("diff_files", tools)
        schema = tools["apply_patch"].tool_schema["function"]
        parameters = schema.get("parameters")
        assert isinstance(parameters, dict)
        properties = parameters.get("properties")
        assert isinstance(properties, dict)
        self.assertEqual(
            set(properties), {"patch", "directory"}
        )
        description = schema.get("description")
        assert isinstance(description, str)
        self.assertIn("diff --git", description)
        result = toolbox.call_tool(
            self.ctx.agent, "apply_patch", {"patch": SAMPLE_PATCH}, None
        )
        message = result.unwrap()
        assert isinstance(message, str)
        self.assertIn("Applied successfully", message)
        self.assertEqual(self.file.read_text(), EXPECTED)
        for parameter in ("strip", "reverse"):
            with self.subTest(parameter=parameter):
                with self.assertRaisesRegex(ValueError, "Invalid arguments"):
                    toolbox.call_tool(
                        self.ctx.agent, "apply_patch",
                        {"patch": SAMPLE_PATCH, parameter: 1}, None,
                    )


if __name__ == "__main__":
    unittest.main()
