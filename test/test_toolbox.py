import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from xun import Agent, NullDisplay
from xun.core.toolbox import ToolBox
from xun.core.toolcall import ToolCallContext
from xun.core.types import ConclusiveToolResult
from xun.core.workspace import Workspace
from xun.tools.framework import framework_respond_with_artifacts


class RespondToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.agent = Agent(display=NullDisplay(), workspace=Workspace(workdir=Path(self._tmp.name)))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _call(self, artifacts, content="all done") -> ConclusiveToolResult:
        ctx = ToolCallContext(cast("Agent[Agent.T.Init]", self.agent), "respond_with_artifacts", None)
        return framework_respond_with_artifacts(ctx, artifacts=artifacts, content=content)

    def test_reports_relative_paths_deduped_with_notes(self) -> None:
        root = Path(self._tmp.name)
        (root / "out").mkdir()
        (root / "out" / "a.txt").write_text("a", encoding="utf-8")
        result = self._call([
            {"path": "out/a.txt", "note": "first"},
            {"path": str(root / "out" / "a.txt")},
            {"path": "./out/a.txt"},
            {"path": "out", "note": "folder"},
        ])
        self.assertEqual(result.content, "all done")
        self.assertEqual(result.meta["artifacts"], [
            {"path": "out/a.txt", "dir": False, "note": "first"},
            {"path": "out", "dir": True, "note": "folder"},
        ])

    def test_rejects_missing_and_escaping_paths(self) -> None:
        with self.assertRaises(ValueError):
            self._call([{"path": "missing.txt"}])
        with self.assertRaises(ValueError):
            self._call([{"path": str(Path(self._tmp.name).parent / "outside.txt")}])


class ToolBoxCloneTest(unittest.TestCase):
    def test_clone_shares_functions_but_isolates_mutable_containers(self) -> None:
        def sample_tool(value: str) -> str:
            """Return the supplied value."""
            return value

        def child_tool() -> str:
            """Return a child-only value."""
            return "child"

        toolbox = ToolBox().register(sample_tool)
        clone = toolbox.clone()

        self.assertIs(toolbox.list_tools()[0], clone.list_tools()[0])

        clone.disable("sample_tool")
        clone.register(child_tool)

        self.assertEqual([tool.name for tool in toolbox.list_tools()], ["sample_tool"])
        self.assertEqual([tool.name for tool in clone.list_tools()], ["child_tool"])


if __name__ == "__main__":
    unittest.main()
