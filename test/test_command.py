import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from xun import Agent, HookArgs, NullDisplay
from xun.command import Command, CommandRegistry
from xun.workspace import Workspace
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


class CommandHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.workdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _new_agent(self) -> Agent:
        return Agent(display=NullDisplay(), workspace=Workspace(workdir=self.workdir))

    def test_before_and_after_command_fire_with_args(self) -> None:
        agent = self._new_agent().initialize()
        seen: list[tuple[str, str, list[str]]] = []
        agent.command.register(Command(
            name="greet",
            handler=lambda a, args: None,
            description="greet",
        ))
        agent.hooks.before_command.add(
            lambda arg: seen.append(("before", arg.command.name, arg.arguments))
        )
        agent.hooks.after_command.add(
            lambda arg: seen.append(("after", arg.command.name, arg.arguments))
        )

        agent.execute_command("greet", "World")

        self.assertEqual(seen, [("before", "greet", ["World"]), ("after", "greet", ["World"])])

    def test_before_command_can_edit_arguments(self) -> None:
        agent = self._new_agent().initialize()
        invoked_args: list[list[str]] = []
        after_args: list[list[str]] = []
        agent.command.register(Command(
            name="echo",
            handler=lambda a, args: invoked_args.append(args),
            description="echo",
        ))

        def rewrite(arg: HookArgs.CommandArgs) -> None:
            arg.arguments = ["edited"]

        agent.hooks.before_command.add(rewrite)
        agent.hooks.after_command.add(lambda arg: after_args.append(arg.arguments))

        agent.execute_command("echo", "original")

        self.assertEqual(invoked_args, [["edited"]])
        self.assertEqual(after_args, [["edited"]])

    def test_unknown_command_does_not_fire_hooks(self) -> None:
        agent = self._new_agent().initialize()
        fired: list[str] = []
        agent.hooks.before_command.add(lambda arg: fired.append("before"))
        agent.hooks.after_command.add(lambda arg: fired.append("after"))

        agent.execute_command("does-not-exist")

        self.assertEqual(fired, [])

    def test_after_command_fires_when_handler_raises(self) -> None:
        agent = self._new_agent().initialize()
        fired: list[str] = []

        def boom(a: Agent, args: list[str]) -> None:
            raise RuntimeError("handler failed")

        agent.command.register(Command(name="boom", handler=boom, description="fails"))
        agent.hooks.after_command.add(lambda arg: fired.append("after"))

        agent.execute_command("boom")  # Command.invoke swallows the error

        self.assertEqual(fired, ["after"])

    def test_malformed_arguments_returns_err_without_firing_hooks(self) -> None:
        agent = self._new_agent().initialize()
        fired: list[str] = []
        agent.command.register(Command(name="greet", handler=lambda a, args: None, description="greet"))
        agent.hooks.before_command.add(lambda arg: fired.append("before"))
        agent.hooks.after_command.add(lambda arg: fired.append("after"))

        # unbalanced quote: shlex.split raises before any hook runs
        res = agent.execute_command("greet", '"unbalanced')

        self.assertTrue(res.is_err())
        self.assertEqual(fired, [])


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
            commands.get("load").invoke(agent, ["latest"])  # type: ignore[arg-type]

        self.assertEqual(agent.conversation.messages[-1]["content"], "saved message")


if __name__ == "__main__":
    unittest.main()