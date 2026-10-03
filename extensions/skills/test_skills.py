import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from .setup_extension import setup_extension
from .tools import activate_skill, read_skill_file
from xun import Agent, Command, NullDisplay, ToolBox
from xun.conversation_message import SystemPrompt
from xun.display_abstract import ShowExtensionsEvent
from xun.display_event import InfoEvent
from xun.hooks import HookArgs
from xun.toolcall import ToolCallContext
from xun.workspace import Workspace


class SkillsTestBase(unittest.TestCase):
    """An initialized agent whose only extension is this one.

    Subclasses set `bundle_before_startup` to decide whether a bundle exists when
    `setup_extension` runs, i.e. whether discovery finds anything.
    """

    bundle_before_startup = False
    REQUIRED_TOOLS = {"list_skills", "activate_skill", "read_skill_file"}

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.workdir = Path(self.temp.name) / "work"
        self.workdir.mkdir()
        if self.bundle_before_startup:
            self.add_bundle(extra_files={"reference.txt": "reference"})
        self.env = patch.dict(os.environ, {"XUN_HOME": str(Path(self.temp.name) / "home")})
        self.env.start()
        agent = Agent(display=NullDisplay(), toolbox=ToolBox(), workspace=Workspace(workdir=self.workdir))
        agent.config.model.name = "test-model"
        self.agent: Agent[Agent.T.Init] = agent.initialize()
        setup_extension(SimpleNamespace(agent=self.agent))  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def add_bundle(self, name: str = "demo", extra_files: dict[str, str] | None = None) -> Path:
        bundle = self.workdir / ".agents" / "skills" / name
        bundle.mkdir(parents=True)
        (bundle / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} instructions.\n---\n# {name.title()}\nDo the thing.\n",
            encoding="utf-8")
        for file, content in (extra_files or {}).items():
            (bundle / file).write_text(content, encoding="utf-8")
        return bundle

    def context(self, tool_name: str = "activate_skill") -> ToolCallContext:
        return ToolCallContext(self.agent, tool_name, None)

    def tool_names(self) -> set[str]:
        return {tool.name for tool in self.agent.toolbox.list_tools()}

    def skill_command(self) -> Command:
        command = self.agent.command.get("skills")
        assert command is not None
        return command

    def infos(self) -> list[str]:
        return [event.payload.message for event in self.agent.display.events() if isinstance(event.payload, InfoEvent)]

    def panels(self) -> list[str]:
        return [event.payload.title for event in self.agent.display.events() if isinstance(event.payload, ShowExtensionsEvent)]

    def prompt(self) -> SystemPrompt:
        prompt = self.agent.conversation.messages[0]
        assert isinstance(prompt, SystemPrompt)
        return prompt

    def catalog(self) -> str:
        return self.prompt().persist_sections.get("agent-skills", "")


class SkillsExtensionTest(SkillsTestBase):
    bundle_before_startup = True

    def test_setup_adds_tools_and_catalog(self) -> None:
        self.assertTrue(self.REQUIRED_TOOLS <= self.tool_names())
        self.assertIn('"name":"demo"', self.catalog())

    def test_activation_is_repeatable_and_updates_catalog(self) -> None:
        first = activate_skill(self.context(), "demo")
        self.assertEqual(first, activate_skill(self.context(), "demo"))
        self.assertIn("Active skills: demo.", self.catalog())

    def test_bundled_files_require_activation_and_cannot_escape(self) -> None:
        with self.assertRaises(ValueError):
            read_skill_file(self.context("read_skill_file"), "demo", "reference.txt")
        activate_skill(self.context(), "demo")
        self.assertEqual(read_skill_file(self.context("read_skill_file"), "demo", "reference.txt"), "reference")
        with self.assertRaises(ValueError):
            read_skill_file(self.context("read_skill_file"), "demo", "../outside.txt")

    def test_skills_command_lists_and_activates_explicitly(self) -> None:
        command = self.skill_command()
        command.invoke(self.agent)
        self.assertEqual(self.panels()[-1], "Skills")
        event = self.agent.display.events()[-1].payload
        self.assertEqual((event.title, event.extensions[0].name), ("Skills", "demo"))
        command.invoke(self.agent, ["activate", "demo"])
        self.assertIn("Active skills: demo.", self.catalog())

    def test_info_does_not_activate_the_skill(self) -> None:
        self.skill_command().invoke(self.agent, ["info", "demo"])
        self.assertNotIn("Active skills", self.catalog())

    def test_catalog_survives_system_replacement_and_serialization(self) -> None:
        self.agent.system("system")
        self.assertIn("agent-skills", self.prompt().persist_sections)
        self.assertIn("persist_sections", json.loads(self.agent.conversation.dumps())["messages"][0])


class LateDiscoveryTest(SkillsTestBase):
    """The catalog is empty at startup, which is when auto resync matters most."""

    def test_command_and_hint_exist_with_an_empty_catalog(self) -> None:
        command = self.skill_command()
        self.assertFalse(self.tool_names() & self.REQUIRED_TOOLS)
        command.invoke(self.agent)
        self.assertIn("No skills discovered", self.infos()[-1])
        self.assertNotIn("Skills", self.panels())

    def test_command_picks_up_a_late_bundle_without_reload(self) -> None:
        self.add_bundle("late")
        self.skill_command().invoke(self.agent)
        self.assertTrue(self.REQUIRED_TOOLS <= self.tool_names())
        self.assertIn('"name":"late"', self.catalog())
        self.assertEqual(self.panels()[-1], "Skills")

    def test_execution_hook_resyncs_without_any_command(self) -> None:
        self.add_bundle("late")
        self.agent.hooks.before_execution.invoke(
            HookArgs.BeforeExecutionArgs(agent=self.agent, schema=None, max_iterations=1))
        self.assertTrue(self.REQUIRED_TOOLS <= self.tool_names())
        self.assertIn('"name":"late"', self.catalog())

    def test_empty_catalog_keeps_the_toolbox_untouched(self) -> None:
        command = self.skill_command()
        command.invoke(self.agent)
        command.invoke(self.agent)
        self.assertFalse(self.tool_names() & self.REQUIRED_TOOLS)

    def test_setup_replay_on_a_subagent_does_not_collide(self) -> None:
        self.add_bundle("late")
        self.skill_command().invoke(self.agent)
        child = Agent.inherit(self.agent).initialize()  # toolbox is a clone of the parent's
        setup_extension(SimpleNamespace(agent=child))  # type: ignore[arg-type]
        self.assertTrue(self.REQUIRED_TOOLS <= {tool.name for tool in child.toolbox.list_tools()})


if __name__ == "__main__":
    unittest.main()
