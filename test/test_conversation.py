import base64
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from xun.conversation import Conversation
from xun.display import input_to_instruction
from xun.display_abstract import MessageInstruction


class ConversationImageInputTest(unittest.TestCase):
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
            image_path.write_bytes(b"png-bytes")

            conversation.add_user_message("", images=[str(image_path)])

        content = cast(list[dict[str, Any]], cast(dict[str, Any], conversation.messages[-1])["content"])
        assert isinstance(content, list)
        image_url = content[0]["image_url"]["url"]
        self.assertEqual(
            image_url,
            f"data:image/png;base64,{base64.b64encode(b'png-bytes').decode('utf-8')}",
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