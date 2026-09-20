import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from xun import Agent, NullDisplay, ToolBox, CommandRegistry
from xun.extension import list_loaded_extensions
from xun.workspace import Workspace
import xun.extension as ext_mod


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class _ExtensionsTestBase(unittest.TestCase):
    """Points XUN_HOME at a fresh temp dir and resets the scan cache per test."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.ext_root = self.home / "extensions"
        self._home_patch = patch.dict(os.environ, {"XUN_HOME": str(self.home)})
        self._home_patch.start()
        ext_mod._scan_extensions.cache_clear()

    def tearDown(self) -> None:
        ext_mod._scan_extensions.cache_clear()
        self._home_patch.stop()
        self._tmp.cleanup()

    def _new_agent(self) -> Agent:
        return Agent(
            display=NullDisplay(),
            workspace=Workspace(workdir=Path(self._tmp.name)),
        )

    def _write_ext(self, name: str, body: str, package: bool = False) -> None:
        target = self.ext_root / (f"{name}/setup_extension.py" if package else f"{name}.py")
        _write(target, body)


class DiscoveryTest(_ExtensionsTestBase):
    def test_both_forms_sorted_by_name(self) -> None:
        self._write_ext("beta", '"""Beta ext."""\ndef setup_extension(ctx): pass\n')
        self._write_ext("alpha", '"""Alpha ext. Long tail ignored."""\ndef setup_extension(ctx): pass\n', package=True)
        loaded = list_loaded_extensions()
        self.assertEqual([e.name for e in loaded], ["alpha", "beta"])
        self.assertEqual(loaded[0].description, "Alpha ext. Long tail ignored.")

    def test_description_defaults_to_first_docstring_line(self) -> None:
        self._write_ext("doc", '"""Line one.\nLine two."""\ndef setup_extension(ctx): pass\n')
        self.assertEqual(list_loaded_extensions()[0].description, "Line one.")

    def test_no_docstring_empty_description(self) -> None:
        self._write_ext("nodoc", "def setup_extension(ctx): pass\n")
        self.assertEqual(list_loaded_extensions()[0].description, "")

    def test_missing_entry_function_skipped(self) -> None:
        self._write_ext("broken", '"""no entry fn here."""\nx = 1\n')
        self.assertEqual(list_loaded_extensions(), [])

    def test_name_collision_package_wins(self) -> None:
        self._write_ext("dup", "def setup_extension(ctx): ctx.agent.state['dup']='flat'\n")
        self._write_ext("dup", "def setup_extension(ctx): ctx.agent.state['dup']='pkg'\n", package=True)
        loaded = list_loaded_extensions()
        self.assertEqual(len(loaded), 1)
        self.assertTrue(str(loaded[0].path).endswith("setup_extension.py"))
        agent = self._new_agent().initialize()
        self.assertEqual(agent.state["dup"], "pkg")

    def test_directory_without_entry_ignored(self) -> None:
        _write(self.ext_root / "notanext" / "other.py", "x = 1\n")
        self.assertEqual(list_loaded_extensions(), [])


class ImportModelTest(_ExtensionsTestBase):
    def test_relative_import_in_package_form(self) -> None:
        _write(self.ext_root / "pkg" / "helper.py", "VALUE = 42\n")
        _write(self.ext_root / "pkg" / "setup_extension.py",
               "from .helper import VALUE\ndef setup_extension(ctx): ctx.agent.state['v'] = VALUE\n")
        agent = self._new_agent().initialize()
        self.assertEqual(agent.state["v"], 42)

    def test_sibling_name_clash_isolated(self) -> None:
        for name in ("one", "two"):
            _write(self.ext_root / name / "utils.py", f"OWNER = '{name}'\n")
            _write(self.ext_root / name / "setup_extension.py",
                   "from .utils import OWNER\ndef setup_extension(ctx): ctx.agent.state[ctx.name] = OWNER\n")
        agent = self._new_agent().initialize()
        self.assertEqual(agent.state["one"], "one")
        self.assertEqual(agent.state["two"], "two")
        self.assertNotIn("utils", sys_modules_names())

    def test_import_error_isolated(self) -> None:
        self._write_ext("boom", "raise RuntimeError('kaboom')\ndef setup_extension(ctx): pass\n")
        self._write_ext("fine", "def setup_extension(ctx): ctx.agent.state['fine'] = True\n")
        loaded = list_loaded_extensions()
        self.assertEqual([e.name for e in loaded], ["fine"])
        agent = self._new_agent().initialize()
        self.assertTrue(agent.state["fine"])
        # half-dead module must not linger in sys.modules
        self.assertNotIn("xun_ext_boom", __import__("sys").modules)


def sys_modules_names() -> list[str]:
    import sys
    return list(sys.modules)


class ApplyTest(_ExtensionsTestBase):
    def test_setup_runs_on_initialize(self) -> None:
        self._write_ext("hookit", """
from xun import ExtensionContext
def setup_extension(ctx: ExtensionContext) -> None:
    ctx.agent.state.setdefault('seen', []).append(ctx.name)
""")
        agent = self._new_agent().initialize()
        self.assertEqual(agent.state["seen"], ["hookit"])

    def test_setup_error_does_not_block(self) -> None:
        self._write_ext("a_bad", "def setup_extension(ctx): raise RuntimeError('x')\n")
        self._write_ext("b_good", "def setup_extension(ctx): ctx.agent.state['ok'] = 1\n")
        agent = self._new_agent().initialize()
        self.assertEqual(agent.state["ok"], 1)

    def test_config_opt_out(self) -> None:
        self._write_ext("gate", "def setup_extension(ctx): ctx.agent.state['ran'] = True\n")
        agent = self._new_agent()
        agent.config.enable_extensions = False
        agent.initialize()
        self.assertNotIn("ran", agent.state)

    def test_subagent_replays_hooks_but_survives_tool_conflict(self) -> None:
        self._write_ext("both", """
from xun import tool_attr
@tool_attr()
def ext_tool() -> str:
    '''ext tool'''
    return 'x'
def setup_extension(ctx):
    # hooks first: on sub-agent replay the tool re-registration conflicts,
    # which aborts the rest of this function via apply_extensions' error isolation
    ctx.agent.hooks.before_tool_call.add(lambda a: ctx.agent.state.setdefault('hits', []).append(1))
    ctx.agent.toolbox.register(ext_tool)
""")
        parent = self._new_agent().initialize()
        self.assertIn("ext_tool", [t.name for t in parent.toolbox.list_tools()])
        child = Agent.inherit(parent).initialize()
        # tool re-registration conflicts are downgraded to a warning, init still succeeds...
        self.assertIn("ext_tool", [t.name for t in child.toolbox.list_tools()])
        # ...while hooks (not inherited) arrive only via replay
        from xun.hooks import HookArgs
        child.hooks.before_tool_call.invoke(HookArgs.BeforeToolCallArgs(agent=child, tool_calls=[]))
        self.assertEqual(child.state.get("hits"), [1])

    def test_cache_survives_across_initializes(self) -> None:
        self._write_ext("once", """
import xun
print('side effect')
n = [0]
def setup_extension(ctx):
    n[0] += 1
    ctx.agent.state['n'] = n[0]
""")
        first = self._new_agent().initialize()
        second = self._new_agent().initialize()
        self.assertEqual(first.state["n"], 1)
        self.assertEqual(second.state["n"], 2)  # module-level state persists, setup replays


class ExtensionsCommandTest(_ExtensionsTestBase):
    class _CapturingAgent:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def info(self, message: str) -> None:
            self.messages.append(message)

    def _run_command(self) -> list[str]:
        agent = self._CapturingAgent()
        CommandRegistry().with_defaults().get("extensions").invoke(agent)  # type: ignore[arg-type]
        return agent.messages

    def test_lists_loaded_extensions(self) -> None:
        self._write_ext("shown", '"""Show me."""\ndef setup_extension(ctx): pass\n')
        msgs = self._run_command()
        self.assertEqual(msgs, ["\nshown: Show me."])

    def test_empty_listing(self) -> None:
        self.assertEqual(self._run_command(), ["No extensions loaded."])

    def test_command_registered_by_default(self) -> None:
        self.assertIsNotNone(CommandRegistry().with_defaults().get("extensions"))


if __name__ == "__main__":
    unittest.main()
