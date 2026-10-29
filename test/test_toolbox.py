import unittest

from xun.core.toolbox import ToolBox


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
