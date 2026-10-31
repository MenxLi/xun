import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from xun import Agent, HookArgs, NullDisplay
from xun.core.agent import T
from xun.core.command import Command, CommandRegistry
from xun.session import register_default_commands
from xun.core.workspace import Workspace
from xun.core.display_abstract import ShowToolsEvent
from xun.core.toolbox import ToolBox
from xun.core.toolcall import tool_attr


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

        tools_command = register_default_commands(CommandRegistry()).get("tools")
        assert tools_command is not None
        tools_command.invoke(cast("Agent[T.Init]", agent))

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

        tools_command = register_default_commands(CommandRegistry()).get("tools")
        assert tools_command is not None
        tools_command.invoke(cast("Agent[T.Init]", agent))

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

        def boom(a: "Agent[T.Init]", args: list[str]) -> None:
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


if __name__ == "__main__":
    unittest.main()