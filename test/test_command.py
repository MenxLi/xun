import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from xun.command import CommandRegistry
from xun.conversation import Conversation
from xun.display_abstract import ShowToolsEvent
from xun.store import Store
from xun.toolbox import ToolBox
from xun.toolcall import tool_attr


class _CapturingAgent:
    def __init__(self, toolbox: ToolBox) -> None:
        self.toolbox = toolbox
        self.events: list[object] = []

    def display_event(self, event: object) -> None:
        self.events.append(event)


class ToolsCommandTest(unittest.TestCase):
    def test_emits_structured_tool_metadata(self) -> None:
        @tool_attr(required_capabilities=["vision"])
        def inspect_image(path: str) -> str:
            """Inspect an image file."""
            return path

        agent = _CapturingAgent(ToolBox().register(inspect_image))

        CommandRegistry().with_defaults().get("tools").invoke(agent)  # type: ignore[arg-type]

        self.assertEqual(len(agent.events), 1)
        event = agent.events[0]
        self.assertIsInstance(event, ShowToolsEvent)
        assert isinstance(event, ShowToolsEvent)
        self.assertEqual(event.model_dump(), {
            "tools": [{
                "name": "inspect_image",
                "description": "Inspect an image file.",
                "required_capabilities": ["vision"],
            }],
        })

    def test_emits_empty_tool_list(self) -> None:
        agent = _CapturingAgent(ToolBox())

        CommandRegistry().with_defaults().get("tools").invoke(agent)  # type: ignore[arg-type]

        self.assertEqual(agent.events, [ShowToolsEvent(tools=[])])


class HistoryCommandTest(unittest.TestCase):
    def test_save_and_load_round_trip(self) -> None:
        agent = _CapturingAgent(ToolBox())
        agent.conversation = Conversation()
        agent.info = lambda _message: None
        agent.error = lambda _message: None
        agent.conversation.add_user_message("saved message")

        with TemporaryDirectory() as directory, patch(
            "xun.store.Store", side_effect=lambda: Store(Path(directory))
        ):
            commands = CommandRegistry().with_defaults()
            commands.get("save").invoke(agent)  # type: ignore[arg-type]
            agent.conversation.clear()
            commands.get("load").invoke(agent, "latest")  # type: ignore[arg-type]

        self.assertEqual(agent.conversation.messages[-1]["content"], "saved message")


if __name__ == "__main__":
    unittest.main()