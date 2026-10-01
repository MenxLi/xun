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
        parts = cast(list[dict[str, Any]], conversation.messages[-1]["content"])
        url = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
        # The processed image (not the original URL) is attached as a data URL.
        self.assertTrue(url.startswith("data:image/png;base64,"))
        image = Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1])))
        self.assertEqual(image.size, (2, 2))

    def test_request_image_crop(self) -> None:
        conversation = Conversation()
        agent = SimpleNamespace(hooks=Hooks(), conversation=conversation)
        context = ToolCallContext(agent, "request_image", None)
        output = BytesIO()
        Image.new("RGB", (100, 100), "blue").save(output, format="PNG")
        response = SimpleNamespace(content=output.getvalue(), raise_for_status=lambda: None)

        def sent_image() -> Image.Image:
            conversation.add_tool_result("call_1", Result.Ok("OK"))
            agent.hooks.after_execution_step.invoke(HookArgs.AfterExecutionStepArgs(agent=agent))
            parts = cast(list[dict[str, Any]], conversation.messages[-1]["content"])
            url = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
            return Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1])))

        with patch("requests.get", return_value=response):
            # crop bottom-left quarter -> original pixels of a 50x50 region
            self.assertEqual(fs_request_image(context, "https://example.com/chart.png", crop=(0.0, 0.5, 0.5, 0.5)), "OK")
        self.assertEqual(sent_image().size, (50, 50))

        with patch("requests.get", return_value=response):
            self.assertEqual(fs_request_image(context, "https://example.com/chart.png", crop=(0.25, 0.25, 0.5, 0.5)), "OK")
        self.assertEqual(sent_image().size, (50, 50))

        for bad_crop in [(0.5, 0.5, 0.6, 0.5), (0.0, 0.0, 0.0, 0.5), (-0.1, 0.0, 0.5, 0.5)]:
            with self.assertRaises(ValueError), patch("requests.get", return_value=response):
                fs_request_image(context, "https://example.com/chart.png", crop=bad_crop)

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