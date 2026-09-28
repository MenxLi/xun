import base64
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from PIL import Image

from xun.conversation import Conversation
from xun.display_abstract import UserMessageEvent
from xun.entrypoint import MessageInstruction, input_to_instruction
from xun.hooks import HookArgs, Hooks
from xun.toolcall import ToolCallContext
from xun.tools.fs import fs_request_image
from xun.types import Result


class ConversationImageInputTest(unittest.TestCase):
    def test_request_image_follows_tool_result(self) -> None:
        conversation = Conversation()
        agent = SimpleNamespace(hooks=Hooks(), conversation=conversation)
        context = ToolCallContext(agent, "request_image", None)
        output = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(output, format="PNG")
        response = SimpleNamespace(content=output.getvalue(), raise_for_status=lambda: None)

        with patch("requests.get", return_value=response) as request:
            self.assertEqual(fs_request_image(context, "https://example.com/chart.png"), "OK")
        request.assert_called_once_with("https://example.com/chart.png", timeout=30)
        self.assertEqual(conversation.messages, [])

        conversation.add_tool_result("call_1", Result.Ok("OK"))
        agent.hooks.after_execution_step.invoke(HookArgs.AfterExecutionStepArgs(agent=agent))

        self.assertEqual([message["role"] for message in conversation.messages], ["tool", "user"])
        self.assertEqual(
            cast(list[dict[str, Any]], conversation.messages[-1]["content"])[0]["image_url"]["url"],
            "https://example.com/chart.png",
        )

    def test_render_history_as_html_expands_json_tool_result_content(self) -> None:
        conversation = Conversation()
        conversation.add_tool_result("call_1", Result.Ok({"os": "Linux", "architecture": "x86_64"}))

        html = conversation.render_history_as_html()

        self.assertIn('"os": "Linux"', html)
        self.assertIn('"architecture": "x86_64"', html)
        self.assertNotIn(r'\"os\"', html)

    def test_render_history_as_html_renders_images_and_message_anchors(self) -> None:
        conversation = Conversation()
        conversation.add_user_message("请分析图片", images=["https://example.com/chart.png"])
        conversation.messages.append({"role": "assistant", "content": "这是分析结果。"})

        html = conversation.render_history_as_html()

        self.assertIn('id="message-1"', html)
        self.assertIn('href="#message-1">#1</a>', html)
        self.assertIn('id="message-2"', html)
        self.assertIn('href="#message-2">#2</a>', html)
        self.assertIn('<img src="https://example.com/chart.png"', html)
        self.assertIn("请分析图片", html)
        self.assertNotIn('&quot;type&quot;: &quot;image_url&quot;', html)

    def test_render_history_as_html_preserves_chinese_in_tool_details(self) -> None:
        conversation = Conversation()
        conversation.add_tool_result("call_1", Result.Ok({"title": "中文测试"}))
        conversation.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "搜索", "arguments": '{"查询": "中文"}'},
                    }
                ],
            }
        )

        html = conversation.render_history_as_html()

        self.assertIn("中文测试", html)
        self.assertIn("搜索", html)
        self.assertIn("查询", html)
        self.assertNotIn(r"\u4e2d\u6587", html)

    def test_add_user_message_keeps_plain_text(self) -> None:
        conversation = Conversation()

        conversation.add_user_message("hello")

        self.assertEqual(conversation.messages[-1], {"role": "user", "content": "hello"})

    def test_add_user_message_supports_image_urls(self) -> None:
        conversation = Conversation()

        conversation.add_user_message(
            "compare them",
            images=["https://example.com/cat.png", "https://example.com/dog.png"],
        )

        self.assertEqual(
            conversation.messages[-1],
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "compare them"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
                    {"type": "image_url", "image_url": {"url": "https://example.com/dog.png"}},
                ],
            },
        )

    def test_add_user_message_encodes_local_file(self) -> None:
        conversation = Conversation()
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "sample.png"
            output = BytesIO()
            Image.new("RGB", (2, 2), "blue").save(output, format="PNG")
            image_path.write_bytes(output.getvalue())

            conversation.add_user_message("", images=[str(image_path)])

        content = cast(list[dict[str, Any]], cast(dict[str, Any], conversation.messages[-1])["content"])
        assert isinstance(content, list)
        image_url = content[0]["image_url"]["url"]
        self.assertEqual(
            image_url,
            f"data:image/png;base64,{base64.b64encode(output.getvalue()).decode('utf-8')}",
        )

    def test_user_message_event_normalizes_images(self) -> None:
        event = UserMessageEvent.from_inputs(
            "compare them",
            images=["https://example.com/cat.png"],
        )

        self.assertEqual(
            event.model_dump(),
            {
                "content": "compare them",
                "images": [{"kind": "url", "value": "https://example.com/cat.png"}],
            },
        )

    def test_history_stringifies_multimodal_user_content(self) -> None:
        conversation = Conversation()
        conversation.add_user_message("what is here", images=["https://example.com/cat.png"])

        history = conversation.to_history()

        self.assertEqual(history[-1]["role"], "user")
        self.assertIn('"type": "image_url"', history[-1]["content"])

    def test_add_user_message_preserves_image_order(self) -> None:
        conversation = Conversation()

        conversation.add_user_message(
            "first\nsecond",
            images=["https://example.com/cat.png"],
        )

        self.assertEqual(
            conversation.messages[-1],
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first\nsecond"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
                ],
            },
        )

    def test_pop_last_message_if_user_removes_multimodal_user_message(self) -> None:
        conversation = Conversation()
        conversation.add_user_message("describe this", images=["https://example.com/cat.png"])

        removed = conversation.pop_last_message_if_user()

        self.assertEqual(
            removed,
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
                ],
            },
        )
        self.assertEqual(conversation.messages, [])


class DisplayMessageInputTest(unittest.TestCase):
    def test_input_to_instruction_parses_images(self) -> None:
        instruction = input_to_instruction(
            "[image:https://example.com/cat.png image:https://example.com/dog.png] compare them"
        )

        self.assertEqual(
            instruction,
            MessageInstruction(
                content="compare them",
                images=["https://example.com/cat.png", "https://example.com/dog.png"],
            ),
        )

    def test_input_to_instruction_keeps_plain_text_when_not_image_syntax(self) -> None:
        instruction = input_to_instruction("[note:todo] compare them")

        self.assertEqual(
            instruction,
            MessageInstruction(content="[note:todo] compare them"),
        )


if __name__ == "__main__":
    unittest.main()