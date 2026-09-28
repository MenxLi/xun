import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from xun import Agent, NullDisplay, ToolBox, CommandRegistry
from xun.display_abstract import ShowExtensionsEvent
from xun.extension import default_loader, ExtensionInfo, ExtensionStatus
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
        ext_mod.default_loader.clear_scan_cache()

    def tearDown(self) -> None:
        ext_mod.default_loader.clear_scan_cache()
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
        imported = default_loader.imported()
        self.assertEqual([e.name for e in imported], ["alpha", "beta"])
        self.assertEqual(imported[0].description, "Alpha ext. Long tail ignored.")

    def test_description_defaults_to_first_docstring_line(self) -> None:
        self._write_ext("doc", '"""Line one.\nLine two."""\ndef setup_extension(ctx): pass\n')
        self.assertEqual(default_loader.imported()[0].description, "Line one.")

    def test_no_docstring_empty_description(self) -> None:
        self._write_ext("nodoc", "def setup_extension(ctx): pass\n")
        self.assertEqual(default_loader.imported()[0].description, "")

    def test_missing_entry_function_skipped(self) -> None:
        self._write_ext("broken", '"""no entry fn here."""\nx = 1\n')
        self.assertEqual(default_loader.imported(), [])

    def test_name_collision_package_wins(self) -> None:
        self._write_ext("dup", "def setup_extension(ctx): ctx.agent.state['dup']='flat'\n")
        self._write_ext("dup", "def setup_extension(ctx): ctx.agent.state['dup']='pkg'\n", package=True)
        imported = default_loader.imported()
        self.assertEqual(len(imported), 1)
        self.assertTrue(str(imported[0].path).endswith("setup_extension.py"))
        agent = self._new_agent().initialize()
        self.assertEqual(agent.state["dup"], "pkg")

    def test_directory_without_entry_ignored(self) -> None:
        _write(self.ext_root / "notanext" / "other.py", "x = 1\n")
        self.assertEqual(default_loader.imported(), [])


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
        imported = default_loader.imported()
        self.assertEqual([e.name for e in imported], ["fine"])
        agent = self._new_agent().initialize()
        self.assertTrue(agent.state["fine"])
        # half-dead module must not linger in sys.modules
        self.assertNotIn("xun_ext_boom", __import__("sys").modules)


def sys_modules_names() -> list[str]:
    import sys
    return list(sys.modules)


class ExtensionStatusTest(_ExtensionsTestBase):
    def _infos(self) -> dict[str, ExtensionInfo]:
        return {info.name: info for info in default_loader.infos()}

    def test_uninitialized_before_setup(self) -> None:
        self._write_ext("idle", '"""Idle."""\ndef setup_extension(ctx): pass\n')
        info = self._infos()["idle"]
        self.assertEqual(info.status, ExtensionStatus.UNINITIALIZED)
        self.assertIsNone(info.error)

    def test_loaded_after_setup(self) -> None:
        self._write_ext("good", '"""Good."""\ndef setup_extension(ctx): pass\n')
        self._new_agent().initialize()
        info = self._infos()["good"]
        self.assertEqual(info.status, ExtensionStatus.LOADED)
        self.assertIsNone(info.error)

    def test_setup_failure_records_status_and_error(self) -> None:
        self._write_ext("bad", "def setup_extension(ctx): raise RuntimeError('boom')\n")
        self._new_agent().initialize()
        info = self._infos()["bad"]
        self.assertEqual(info.status, ExtensionStatus.FAILED)
        self.assertIn("boom", info.error)

    def test_import_failure_listed_as_failed(self) -> None:
        self._write_ext("broken", '"""no entry fn."""\nx = 1\n')
        info = self._infos()["broken"]
        self.assertEqual(info.status, ExtensionStatus.FAILED)
        self.assertIn("setup_extension", info.error)

    def test_import_error_listed_as_failed(self) -> None:
        self._write_ext("boom", "raise RuntimeError('kaboom')\n")
        info = self._infos()["boom"]
        self.assertEqual(info.status, ExtensionStatus.FAILED)
        self.assertIn("kaboom", info.error)

    def test_setup_error_survives_later_success(self) -> None:
        # a FAILED status must not be erased by unrelated later runs
        self._write_ext("bad", "def setup_extension(ctx): raise RuntimeError('boom')\n")
        self._new_agent().initialize()
        self._write_ext("good", "def setup_extension(ctx): pass\n")
        ext_mod.default_loader.clear_scan_cache()
        self._new_agent().initialize()
        self.assertEqual(self._infos()["bad"].status, ExtensionStatus.FAILED)


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
            self.events: list[object] = []
            # mirror the Agent field's default
            self.extension_loader = default_loader

        def display_event(self, event: object) -> None:
            self.events.append(event)

    def _run_command(self) -> ShowExtensionsEvent:
        agent = self._CapturingAgent()
        CommandRegistry().with_defaults().get("extensions").invoke(agent)  # type: ignore[arg-type]
        self.assertEqual(len(agent.events), 1)
        event = agent.events[0]
        self.assertIsInstance(event, ShowExtensionsEvent)
        assert isinstance(event, ShowExtensionsEvent)
        return event

    def test_emits_status_records(self) -> None:
        self._write_ext("shown", '"""Show me."""\ndef setup_extension(ctx): pass\n')
        self._write_ext("broken", '"""no entry fn."""\nx = 1\n')
        event = self._run_command()
        self.assertEqual(event.model_dump(), {"extensions": [
            {"name": "broken", "description": "", "status": "failed", "error": "has no callable 'setup_extension()'"},
            {"name": "shown", "description": "Show me.", "status": "uninitialized", "error": None},
        ]})

    def test_empty_listing(self) -> None:
        self.assertEqual(self._run_command().model_dump(), {"extensions": []})

    def test_command_registered_by_default(self) -> None:
        self.assertIsNotNone(CommandRegistry().with_defaults().get("extensions"))


if __name__ == "__main__":
    unittest.main()
