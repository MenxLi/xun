import unittest
from pathlib import Path
from unittest.mock import Mock

from xun.display_abstract import (
    AgentInfo,
    AgentRunningEndEvent,
    AgentRunningStartEvent,
    ConfirmEvent,
    DisplayEvent,
    ModelMessageEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageEvent,
)
from xun.displays.display import Display


class DisplayTest(unittest.TestCase):
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

    def test_render_uses_buffered_display_events(self) -> None:
        display = Display()
        display._record_event(_ev(UserMessageEvent(content="hello", images=[])))

        html = display.render_history_as_html(title="xun · agent")

        self.assertIn("hello", html)
        self.assertIn("<title>xun · agent</title>", html)


if __name__ == "__main__":
    unittest.main()