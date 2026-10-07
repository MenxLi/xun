import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from xun import Agent, JsonEntry, NullDisplay, ToolBox, CommandRegistry
from xun.display_abstract import ShowExtensionsEvent
from xun.extension import default_loader, ExtensionInfo, ExtensionStatus
from xun.workspace import Workspace
import xun.extension as ext_mod


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _json_value(agent: Agent, key: str):
    entry = agent.state[key]
    assert isinstance(entry, JsonEntry)
    return entry.value


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
        self._write_ext("nodoc", "def setup_extension(ctx): pass\n")
        imported = {e.name: e.description for e in default_loader.imported()}
        self.assertEqual(imported["doc"], "Line one.")
        self.assertEqual(imported["nodoc"], "")

    def test_missing_entry_function_skipped(self) -> None:
        self._write_ext("broken", '"""no entry fn here."""\nx = 1\n')
        self.assertEqual(default_loader.imported(), [])

    def test_name_collision_package_wins(self) -> None:
        self._write_ext("dup", "from xun import JsonEntry\ndef setup_extension(ctx): ctx.agent.state['dup']=JsonEntry('flat')\n")
        self._write_ext("dup", "from xun import JsonEntry\ndef setup_extension(ctx): ctx.agent.state['dup']=JsonEntry('pkg')\n", package=True)
        imported = default_loader.imported()
        self.assertEqual(len(imported), 1)
        self.assertTrue(str(imported[0].path).endswith("setup_extension.py"))
        agent = self._new_agent().initialize()
        self.assertEqual(_json_value(agent, "dup"), "pkg")

    def test_directory_without_entry_ignored(self) -> None:
        _write(self.ext_root / "notanext" / "other.py", "x = 1\n")
        self.assertEqual(default_loader.imported(), [])


class ImportModelTest(_ExtensionsTestBase):
    def test_relative_import_in_package_form(self) -> None:
        _write(self.ext_root / "pkg" / "helper.py", "VALUE = 42\n")
        _write(self.ext_root / "pkg" / "setup_extension.py",
               "from xun import JsonEntry\nfrom .helper import VALUE\ndef setup_extension(ctx): ctx.agent.state['v'] = JsonEntry(VALUE)\n")
        agent = self._new_agent().initialize()
        self.assertEqual(_json_value(agent, "v"), 42)

    def test_sibling_name_clash_isolated(self) -> None:
        for name in ("one", "two"):
            _write(self.ext_root / name / "utils.py", f"OWNER = '{name}'\n")
            _write(self.ext_root / name / "setup_extension.py",
                   "from xun import JsonEntry\nfrom .utils import OWNER\ndef setup_extension(ctx): ctx.agent.state[ctx.name] = JsonEntry(OWNER)\n")
        agent = self._new_agent().initialize()
        self.assertEqual(_json_value(agent, "one"), "one")
        self.assertEqual(_json_value(agent, "two"), "two")
        self.assertNotIn("utils", sys_modules_names())

    def test_import_error_isolated(self) -> None:
        self._write_ext("boom", "raise RuntimeError('kaboom')\ndef setup_extension(ctx): pass\n")
        self._write_ext("fine", "from xun import JsonEntry\ndef setup_extension(ctx): ctx.agent.state['fine'] = JsonEntry(True)\n")
        imported = default_loader.imported()
        self.assertEqual([e.name for e in imported], ["fine"])
        agent = self._new_agent().initialize()
        self.assertTrue(_json_value(agent, "fine"))
        # half-dead module must not linger in sys.modules
        self.assertNotIn("xun_ext_boom", __import__("sys").modules)


class DataDirTest(_ExtensionsTestBase):
    def test_data_dir_created_and_namespaced(self) -> None:
        self._write_ext("created", """
from xun import ExtensionContext, JsonEntry
def setup_extension(ctx: ExtensionContext) -> None:
    ctx.agent.state[ctx.name] = JsonEntry(str(ctx.data_dir()))
""")
        self._write_ext("dry", """
from xun import ExtensionContext, JsonEntry
def setup_extension(ctx: ExtensionContext) -> None:
    ctx.agent.state[ctx.name] = JsonEntry(str(ctx.data_dir(_create=False)))
""")
        agent = self._new_agent().initialize()
        created = _json_value(agent, "created")
        dry = _json_value(agent, "dry")
        self.assertEqual(created, str(self.home / "extension_data" / "created"))
        self.assertEqual(dry, str(self.home / "extension_data" / "dry"))
        self.assertTrue(Path(created).is_dir())
        # _create=False resolves the path without touching disk
        self.assertFalse(Path(dry).exists())


def sys_modules_names() -> list[str]:
    import sys
    return list(sys.modules)


class ExtensionStatusTest(_ExtensionsTestBase):
    def _infos(self) -> dict[str, ExtensionInfo]:
        return {info.name: info for info in default_loader.infos()}

    def test_status_lifecycle_uninitialized_to_loaded(self) -> None:
        self._write_ext("idle", '"""Idle."""\ndef setup_extension(ctx): pass\n')
        info = self._infos()["idle"]
        self.assertEqual(info.status, ExtensionStatus.UNINITIALIZED)
        self.assertIsNone(info.reason)
        self._new_agent().initialize()
        info = self._infos()["idle"]
        self.assertEqual(info.status, ExtensionStatus.LOADED)
        self.assertIsNone(info.reason)

    def test_setup_failure_records_status_and_survives_later_success(self) -> None:
        # a FAILED status must not be erased by unrelated later runs
        self._write_ext("bad", "def setup_extension(ctx): raise RuntimeError('boom')\n")
        self._new_agent().initialize()
        info = self._infos()["bad"]
        self.assertEqual(info.status, ExtensionStatus.FAILED)
        self.assertIn("boom", info.reason)
        self._write_ext("good", "def setup_extension(ctx): pass\n")
        ext_mod.default_loader.clear_scan_cache()
        self._new_agent().initialize()
        self.assertEqual(self._infos()["bad"].status, ExtensionStatus.FAILED)

    def test_import_failures_listed_as_failed(self) -> None:
        self._write_ext("noentry", '"""no entry fn."""\nx = 1\n')
        self._write_ext("boom", "raise RuntimeError('kaboom')\n")
        infos = self._infos()
        self.assertEqual(infos["noentry"].status, ExtensionStatus.FAILED)
        self.assertIn("setup_extension", infos["noentry"].reason)
        self.assertEqual(infos["boom"].status, ExtensionStatus.FAILED)
        self.assertIn("kaboom", infos["boom"].reason)


class ApplyTest(_ExtensionsTestBase):
    def test_setup_runs_on_initialize(self) -> None:
        self._write_ext("hookit", """
from xun import ExtensionContext, JsonEntry
def setup_extension(ctx: ExtensionContext) -> None:
    ctx.agent.state.get_entry('seen', lambda: JsonEntry([])).value.append(ctx.name)
""")
        agent = self._new_agent().initialize()
        self.assertEqual(_json_value(agent, "seen"), ["hookit"])

    def test_setup_error_does_not_block(self) -> None:
        self._write_ext("a_bad", "def setup_extension(ctx): raise RuntimeError('x')\n")
        self._write_ext("b_good", "from xun import JsonEntry\ndef setup_extension(ctx): ctx.agent.state['ok'] = JsonEntry(1)\n")
        agent = self._new_agent().initialize()
        self.assertEqual(_json_value(agent, "ok"), 1)

    def test_config_opt_out(self) -> None:
        self._write_ext("gate", "from xun import JsonEntry\ndef setup_extension(ctx): ctx.agent.state['ran'] = JsonEntry(True)\n")
        agent = self._new_agent()
        agent.config.enable_extensions = False
        agent.initialize()
        self.assertNotIn("ran", agent.state)

    def test_extension_settings_namespaced(self) -> None:
        self._write_ext("cfg", "from xun import JsonEntry\ndef setup_extension(ctx): ctx.agent.state['cfg'] = JsonEntry(ctx.settings)\n")
        self._write_ext("bare", "from xun import JsonEntry\ndef setup_extension(ctx): ctx.agent.state['bare'] = JsonEntry(ctx.settings)\n")
        agent = self._new_agent()
        agent.config.extension_settings = {"cfg": {"base_url": "https://x"}}
        agent.initialize()
        self.assertEqual(_json_value(agent, "cfg"), {"base_url": "https://x"})
        self.assertEqual(_json_value(agent, "bare"), {})

    def test_subagent_replays_hooks_but_survives_tool_conflict(self) -> None:
        self._write_ext("both", """
from xun import tool_attr, JsonEntry
@tool_attr()
def ext_tool() -> str:
    '''ext tool'''
    return 'x'
def setup_extension(ctx):
    # hooks first: on sub-agent replay the tool re-registration conflicts,
    # which aborts the rest of this function via apply_extensions' error isolation
    ctx.agent.hooks.before_tool_call.add(lambda a: ctx.agent.state.get_entry('hits', lambda: JsonEntry([])).value.append(1))
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
        self.assertEqual(_json_value(child, "hits"), [1])

    def test_cache_survives_across_initializes(self) -> None:
        self._write_ext("once", """
import xun
print('side effect')
n = [0]
def setup_extension(ctx):
    n[0] += 1
    ctx.agent.state['n'] = xun.JsonEntry(n[0])
""")
        first = self._new_agent().initialize()
        second = self._new_agent().initialize()
        self.assertEqual(_json_value(first, "n"), 1)
        self.assertEqual(_json_value(second, "n"), 2)  # module-level state persists, setup replays


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
        self.assertEqual(event.model_dump(), {"title": "Extensions", "extensions": [
            {"name": "broken", "description": "", "status": "failed", "reason": "has no callable 'setup_extension()'", "source": None},
            {"name": "shown", "description": "Show me.", "status": "uninitialized", "reason": None, "source": None},
        ]})

    def test_empty_listing(self) -> None:
        self.assertEqual(self._run_command().model_dump(), {"title": "Extensions", "extensions": []})

    def test_command_registered_by_default(self) -> None:
        self.assertIsNotNone(CommandRegistry().with_defaults().get("extensions"))


GATED = """
from xun import extension_attr, JsonEntry
@extension_attr(api_min_version='{lo}', api_max_version='{hi}')
def setup_extension(ctx): ctx.agent.state['gated'] = JsonEntry(True)
"""

class VersionGateTest(_ExtensionsTestBase):
    def _write_gated(self, name: str = "gated") -> None:
        self._write_ext(name, GATED.format(lo="2.0", hi="3.0"))

    def test_attr_reaches_extension_struct(self) -> None:
        self._write_gated()
        with patch.object(ext_mod, "xun_version", return_value=None):
            ext = default_loader.imported()[0]
        self.assertEqual((ext.api_min_version, ext.api_max_version), ("2.0", "3.0"))

    def test_in_range_loads(self) -> None:
        self._write_gated()
        with patch.object(ext_mod, "xun_version", return_value="2.5"):
            agent = self._new_agent().initialize()
        self.assertTrue(_json_value(agent, "gated"))
        self.assertEqual(self._info("gated").status, ExtensionStatus.LOADED)

    def test_out_of_range_skips_without_failing(self) -> None:
        self._write_gated()
        with patch.object(ext_mod, "xun_version", return_value="1.5.0"):
            agent = self._new_agent().initialize()
        self.assertNotIn("gated", agent.state)
        info = self._info("gated")
        self.assertEqual(info.status, ExtensionStatus.SKIPPED)
        self.assertEqual(info.reason, "requires xun >= 2.0, running 1.5.0")

    def test_without_version_metadata_passes(self) -> None:
        self._write_gated()
        with patch.object(ext_mod, "xun_version", return_value=None):
            agent = self._new_agent().initialize()
        self.assertTrue(_json_value(agent, "gated"))

    def test_version_conflict_matrix(self) -> None:
        ext = lambda lo, hi: ext_mod.Extension(name="e", description="", setup=lambda ctx: None,
                                               path=Path("e.py"), api_min_version=lo, api_max_version=hi)
        with patch.object(ext_mod, "xun_version", return_value="1.2.3"):
            self.assertIsNone(ext(None, None).version_conflict())
            self.assertIsNone(ext("1.0", "2.0").version_conflict())
            self.assertIsNone(ext("1.2.3", "1.2.3").version_conflict())  # bounds inclusive
            self.assertIsNone(ext("1.2", None).version_conflict())  # 1.2.3 > 1.2
            self.assertIsNotNone(ext("2.0", None).version_conflict())
            self.assertIsNotNone(ext(None, "1.0").version_conflict())

    def test_version_compare_cores(self) -> None:
        self.assertEqual(ext_mod._compare_versions("1.2", "1.2.0"), 0)  # zero-padded
        self.assertEqual(ext_mod._compare_versions("1.10", "1.9"), 1)
        self.assertEqual(ext_mod._compare_versions("2.0rc1", "1.9"), 1)  # numeric core

    def _info(self, name: str) -> ExtensionInfo:
        return {info.name: info for info in default_loader.infos()}[name]


if __name__ == "__main__":
    unittest.main()
