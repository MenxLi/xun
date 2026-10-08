import unittest
import threading
import io
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.application import get_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
import rich.console

from xun.display_abstract import (
    AgentBindEvent,
    AgentInfo,
    AgentRunningEndEvent,
    AgentRunningStartEvent,
    AgentUnbindEvent,
    ConfirmEvent,
    DisplayAbstract,
    DisplayEvent,
    ModelMessageEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from xun.displays.cli_session import (
    _ActivePrompt,
    CliCompleter,
    CliSession,
    FileMentionCompleter,
    SlashCommandCompleter,
    _PromptInterrupted,
    _PromptTakeover,
)
from xun.displays.display import Display


class DisplayTest(unittest.TestCase):
    def test_choice_context_redraws_after_background_event(self) -> None:
        started = threading.Event()
        redrawn = threading.Event()

        class TerminalOutput(io.StringIO):
            def write(self, text: str) -> int:
                result = super().write(text)
                output = self.getvalue()
                if "Request body" in output:
                    started.set()
                if "Request body" in output.partition("background")[2]:
                    redrawn.set()
                return result

        output = TerminalOutput()
        terminal = Vt100_Output(output, lambda: Size(rows=40, columns=100), enable_cpr=False)
        results: list[str] = []
        with create_pipe_input() as pipe, patch.object(
            get_app_session(), "_input", pipe,
        ), patch.object(get_app_session(), "_output", terminal):
            display = Display()
            request = DisplayAbstract.ChoiceRequest(
                agent_info=AGENT, prompt="Allow?", choices=["Yes", "No"], message="Request body",
                title="Permission", subtitle="Details",
            )

            def choose() -> None:
                results.append(display.get_choice(request))

            thread = threading.Thread(target=choose)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                pipe.send_text("\x1b[B")
                display.on_event(_ev(ModelMessageEvent(
                    model_call_id="m1", content="background", total_tokens=1,
                )))
                self.assertTrue(redrawn.wait(3), repr(output.getvalue()))
                restored = output.getvalue().partition("background")[2]
                for text in ("Permission", "Details", "Allow?"):
                    self.assertIn(text, restored)
            finally:
                pipe.send_text("\r")
                thread.join(3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["No"])

    def test_events_render_during_console_input(self) -> None:
        display = Display()
        output = io.StringIO()
        display.console = rich.console.Console(file=output, force_terminal=True)
        input_started = threading.Event()
        release_input = threading.Event()

        def blocking_prompt(*_args, **_kwargs) -> str:
            input_started.set()
            self.assertTrue(release_input.wait(1))
            return "hello"

        with patch.object(display.session._main_session, "prompt", side_effect=blocking_prompt):
            input_thread = threading.Thread(target=display.input, args=(">>> ",))
            input_thread.start()
            self.assertTrue(input_started.wait(1))

            event_thread = threading.Thread(target=display.on_event, args=(_ev(ModelMessageEvent(
                model_call_id="m1", content="background", total_tokens=1,
            )),))
            event_thread.start()
            event_thread.join(1)

            self.assertFalse(event_thread.is_alive())
            self.assertIn("background", output.getvalue())

            release_input.set()
            input_thread.join(1)

        self.assertFalse(input_thread.is_alive())

    def test_running_events_are_ignored(self) -> None:
        display = Display()
        display._unhandled = Mock()
        agent = AgentInfo(name="Xun", identifier="agent-1", workdir=Path.cwd())

        for payload in (AgentRunningStartEvent(), AgentRunningEndEvent()):
            display.on_event(DisplayEvent(
                name=payload.__class__.__name__,
                agent=agent,
                payload=payload,
            ))

        display._unhandled.assert_not_called()


class CliSessionTest(unittest.TestCase):
    def test_confirm_takeover_saves_console_input(self) -> None:
        session = CliSession()
        active_app = Mock()
        active_app.is_running = True
        active_app.current_buffer.document = Document("draft", cursor_position=2)
        active_app.loop.call_soon_threadsafe.side_effect = lambda callback: callback()
        ready = threading.Event()
        ready.set()
        session._active_prompt = _ActivePrompt(active_app, ready, preemptible=True)

        takeover = session._request_takeover()

        self.assertIsNotNone(takeover)
        assert takeover is not None
        self.assertEqual(takeover.document, Document("draft", cursor_position=2))
        interrupted = active_app.exit.call_args.kwargs["exception"]
        self.assertIs(interrupted.takeover, takeover)

    def test_confirm_waits_for_prompt_start_before_takeover(self) -> None:
        session = CliSession()
        active_app = Mock()
        active_app.is_running = False
        active_app.loop = None
        ready = threading.Event()
        session._active_prompt = _ActivePrompt(active_app, ready, preemptible=True)
        takeovers: list[_PromptTakeover | None] = []

        takeover_thread = threading.Thread(target=lambda: takeovers.append(session._request_takeover()))
        takeover_thread.start()
        takeover_thread.join(0.05)
        self.assertTrue(takeover_thread.is_alive())

        active_app.is_running = True
        active_app.loop = Mock()
        active_app.loop.call_soon_threadsafe.side_effect = lambda callback: callback()
        active_app.current_buffer.document = Document("draft")
        ready.set()
        takeover_thread.join(1)

        self.assertFalse(takeover_thread.is_alive())
        self.assertIsNotNone(takeovers[0])
        active_app.exit.assert_called_once()

    def test_console_input_resumes_after_confirm_takeover(self) -> None:
        session = CliSession()
        takeover = _PromptTakeover(document=Document("draft", cursor_position=2))
        result: list[str] = []

        with patch.object(
            session,
            "_prompt",
            side_effect=[_PromptInterrupted(takeover), "draft"],
        ) as prompt:
            input_thread = threading.Thread(target=lambda: result.append(session.input(">>> ")))
            input_thread.start()
            self.assertTrue(takeover.released.wait(1))
            self.assertTrue(input_thread.is_alive())

            takeover.done.set()
            input_thread.join(1)

        self.assertEqual(result, ["draft"])
        self.assertEqual(prompt.call_args_list[1].args[2], takeover.document)
        self.assertFalse(input_thread.is_alive())

    def test_main_prompt_waits_for_pending_takeover(self) -> None:
        session = CliSession()
        results: list[str] = []
        with session._state_changed:
            session._takeover_waiters = 1

        with patch.object(session._main_session, "prompt", return_value="hello") as prompt:
            input_thread = threading.Thread(target=lambda: results.append(session.input(">>> ")))
            input_thread.start()
            input_thread.join(0.05)
            self.assertTrue(input_thread.is_alive())
            prompt.assert_not_called()

            with session._state_changed:
                session._takeover_waiters = 0
                session._state_changed.notify_all()
            input_thread.join(1)

        self.assertEqual(results, ["hello"])
        self.assertFalse(input_thread.is_alive())

    def test_slash_command_completion_uses_current_commands(self) -> None:
        commands = [("help", "Show help")]
        completer = SlashCommandCompleter(lambda: commands)

        first = list(completer.get_completions(Document("/he"), CompleteEvent()))
        commands.append(("history", "Show history"))
        second = list(completer.get_completions(Document("/hi"), CompleteEvent()))

        self.assertEqual([(item.text, item.display_meta_text) for item in first], [("help", "Show help")])
        self.assertEqual([(item.text, item.display_meta_text) for item in second], [("history", "Show history")])

    def test_file_mention_completes_workdir_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("")
            (root / "README.md").write_text("")
            completer = FileMentionCompleter(lambda: root)

            def complete(text: str, after: str = "") -> list[tuple[str, str]]:
                doc = Document(text + after, cursor_position=len(text))
                return [(c.text, c.display_meta_text) for c in completer.get_completions(doc, CompleteEvent())]

            self.assertEqual(complete("analyze @"), [("README.md ", "file"), ("src/", "dir")])
            # substring match, like the web composer
            self.assertEqual(complete("@readme"), [("README.md ", "file")])
            self.assertEqual(complete("@src/"), [("main.py ", "file")])
            # trailing whitespace already present: no extra space appended
            self.assertEqual(complete("@src/", " "), [("main.py", "file")])
            # no trigger, invalid directory, missing root: nothing to complete
            self.assertEqual(complete("analyze src/"), [])
            self.assertEqual(complete("@nope/"), [])
            self.assertEqual(list(FileMentionCompleter(lambda: None).get_completions(Document("@"), CompleteEvent())), [])

    def test_input_sets_completion_root_per_call(self) -> None:
        session = CliSession()
        with tempfile.TemporaryDirectory() as tmp:
            agent = Mock()
            agent.workspace.workdir = Path(tmp)
            with patch.object(session._main_session, "prompt", return_value="hello"):
                self.assertEqual(session.input(">>> ", agent=agent), "hello")
                self.assertEqual(session._prompt_workdir(), None)
                self.assertEqual(session.input(">>> "), "hello")

    def test_cli_completer_serves_both_triggers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "notes.txt").write_text("")
            completer = CliCompleter(lambda: [("help", "Show help")], lambda: Path(tmp))
            slash = list(completer.get_completions(Document("/he"), CompleteEvent()))
            file_ref = list(completer.get_completions(Document("see @not"), CompleteEvent()))
            self.assertEqual([c.display_meta_text for c in slash], ["Show help"])
            self.assertEqual([c.text for c in file_ref], ["notes.txt "])

    def test_choose_uses_prompt_toolkit_choice(self) -> None:
        session = CliSession()
        with patch("xun.displays.cli_session.prompt_choice", return_value=2) as prompt_choice:
            self.assertEqual(session.choose("Choose: ", ["Yes", "No"], default=2), 2)

        self.assertEqual(prompt_choice.call_args.kwargs["options"], [(1, "Yes"), (2, "No")])
        self.assertEqual(prompt_choice.call_args.kwargs["default"], 2)

    def test_choose_rejects_empty_choices_or_invalid_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "At least one choice"):
            CliSession().choose("Choose: ", [])
        with self.assertRaisesRegex(ValueError, "Default choice is out of range"):
            CliSession().choose("Choose: ", ["Yes", "No"], default=3)


AGENT = AgentInfo(name="Xun", identifier="agent-1", workdir=Path.cwd())


def _ev(payload):
    return DisplayEvent(name=payload.__class__.__name__, agent=AGENT, payload=payload)


def _render(events, title="Session") -> str:
    display = Display()
    for event in events:
        display._record_event(event)
    return display.render_history_as_html(title=title)


class RenderHistoryAsHtmlTest(unittest.TestCase):
    def test_tool_call_result_pairing_expands_json(self) -> None:
        events = [
            _ev(ToolCallEvent(tool_call_id="call_1", tool_name="check_env", args={"full": True})),
            _ev(ToolResultEvent(tool_call_id="call_1", result={"os": "Linux", "architecture": "x86_64"})),
        ]

        html = _render(events)

        self.assertIn("check_env", html)
        self.assertIn('"os": "Linux"', html)
        self.assertIn('"architecture": "x86_64"', html)
        self.assertNotIn(r'\"os\"', html)

    def test_user_images_and_assistant_message_render(self) -> None:
        events = [
            _ev(UserMessageEvent.from_inputs("请分析图片", images=["https://example.com/chart.png"])),
            _ev(ModelMessageEvent(model_call_id="m1", content="这是分析结果。", total_tokens=123)),
        ]

        html = _render(events)

        self.assertIn('<img src="https://example.com/chart.png"', html)
        self.assertIn("请分析图片", html)
        self.assertIn("这是分析结果。", html)
        self.assertNotIn("image_url", html)

    def test_preserves_chinese_in_tool_details(self) -> None:
        events = [
            _ev(ToolCallEvent(tool_call_id="call_2", tool_name="搜索", args={"查询": "中文"})),
            _ev(ToolResultEvent(tool_call_id="call_2", result={"title": "中文测试"})),
        ]

        html = _render(events)

        self.assertIn("中文测试", html)
        self.assertIn("搜索", html)
        self.assertIn("查询", html)
        self.assertNotIn(r"\u4e2d\u6587", html)

    def test_activity_groups_collapse_into_batch(self) -> None:
        events = [
            _ev(UserMessageEvent(content="run it", images=[])),
            _ev(ConfirmEvent(choice="Yes", choices=["Yes", "No"], source="auto", prompt="Allow?")),
            _ev(ToolCallEvent(tool_call_id="c1", tool_name="exec", args={"cmd": "ls"})),
            _ev(ToolResultEvent(tool_call_id="c1", result="ok")),
            _ev(ModelMessageEvent(model_call_id="m1", content="done", total_tokens=42)),
        ]

        html = _render(events)

        # user message stands alone; the auto-confirm and tool call collapse into one batch
        self.assertIn("activity-batch", html)
        self.assertIn("1 auto-confirmed", html)
        self.assertIn("1 details", html)
        self.assertIn("done", html)

    def test_agent_lifecycle_events_render_as_pills(self) -> None:
        events = [
            _ev(AgentBindEvent()),
            _ev(AgentUnbindEvent()),
        ]

        html = _render(events)

        self.assertIn('class="agent-lifecycle bound"', html)
        self.assertIn("Xun joined", html)
        self.assertIn("Xun left", html)

    def test_render_uses_buffered_display_events(self) -> None:
        display = Display()
        display._record_event(_ev(UserMessageEvent(content="hello", images=[])))

        html = display.render_history_as_html(title="xun · agent")

        self.assertIn("hello", html)
        self.assertIn("<title>xun · agent</title>", html)


if __name__ == "__main__":
    unittest.main()