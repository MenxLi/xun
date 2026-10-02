import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from .setup_extension import setup_extension
from .tools import activate_skill, read_skill_file
from xun import Agent, NullDisplay, ToolBox
from xun.conversation_message import SystemPrompt
from xun.toolcall import ToolCallContext
from xun.workspace import Workspace


class SkillsExtensionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.workdir = Path(self.temp.name) / "work"
        self.workdir.mkdir()
        self.home = Path(self.temp.name) / "home"
        self.skill_dir = self.workdir / ".agents" / "skills" / "demo"
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text("---\nname: demo\ndescription: Demo instructions.\n---\n# Demo\nDo the thing.\n")
        (self.skill_dir / "reference.txt").write_text("reference")
        self.env = patch.dict(os.environ, {"XUN_HOME": str(self.home)})
        self.env.start()
        agent = Agent(display=NullDisplay(), toolbox=ToolBox(), workspace=Workspace(workdir=self.workdir))
        agent.config.model.name = "test-model"
        self.agent: Agent[Agent.T.Init] = agent.initialize()
        setup_extension(SimpleNamespace(agent=self.agent))  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def context(self, tool_name: str = "activate_skill") -> ToolCallContext:
        return ToolCallContext(self.agent, tool_name, None)

    def prompt(self) -> SystemPrompt:
        prompt = self.agent.conversation.messages[0]
        assert isinstance(prompt, SystemPrompt)
        return prompt

    def test_setup_adds_tools_and_catalog(self) -> None:
        names = {tool.name for tool in self.agent.toolbox.list_tools()}
        self.assertTrue({"list_skills", "activate_skill", "read_skill_file"} <= names)
        self.assertIn('"name":"demo"', self.prompt().persist_sections["agent-skills"])

    def test_activation_is_repeatable_and_updates_catalog(self) -> None:
        first = activate_skill(self.context(), "demo")
        self.assertEqual(first, activate_skill(self.context(), "demo"))
        self.assertIn("Active skills: demo.", self.prompt().persist_sections["agent-skills"])

    def test_bundled_files_require_activation_and_cannot_escape(self) -> None:
        with self.assertRaises(ValueError):
            read_skill_file(self.context("read_skill_file"), "demo", "reference.txt")
        activate_skill(self.context(), "demo")
        self.assertEqual(read_skill_file(self.context("read_skill_file"), "demo", "reference.txt"), "reference")
        with self.assertRaises(ValueError):
            read_skill_file(self.context("read_skill_file"), "demo", "../outside.txt")

    def test_skills_command_lists_and_activates_explicitly(self) -> None:
        command = self.agent.command.get("skill")
        assert command is not None
        command.invoke(self.agent)
        event = self.agent.display.events()[-1].payload
        self.assertEqual((event.title, event.extensions[0].name), ("Skills", "demo"))
        command.invoke(self.agent, ["activate", "demo"])
        self.assertIn("Active skills: demo.", self.prompt().persist_sections["agent-skills"])

    def test_info_does_not_activate_the_skill(self) -> None:
        command = self.agent.command.get("skill")
        assert command is not None
        command.invoke(self.agent, ["info", "demo"])
        self.assertNotIn("Active skills", self.prompt().persist_sections["agent-skills"])

    def test_catalog_survives_system_replacement_and_serialization(self) -> None:
        self.agent.system("system")
        self.assertIn("agent-skills", self.prompt().persist_sections)
        self.assertIn("persist_sections", json.loads(self.agent.conversation.dumps())["messages"][0])


if __name__ == "__main__":
    unittest.main()