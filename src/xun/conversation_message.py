"""
Message types for `Conversation`: each class owns its OpenAI param materialization
(`completion_param`) and persistence format (`to_json`), `message_from_json` is the
factory. Must not import other xun modules: those import this one.
"""
from __future__ import annotations
from openai.types import chat
from typing import Any
from dataclasses import dataclass, field
from abc import ABC, abstractmethod


def remove_empty_tool_calls(message: Any) -> Any:
    # some provider does not allow empty list for tool_calls
    if not isinstance(message, dict):
        return message

    sanitized = dict(message)
    if sanitized.get("tool_calls") == []:
        sanitized.pop("tool_calls", None)
    return sanitized


class AbstractMessage(ABC):
    @property
    @abstractmethod
    def role(self) -> str:
        ...

    @abstractmethod
    def completion_param(self) -> chat.chat_completion_message_param.ChatCompletionMessageParam:
        """Build the OpenAI request param. The result may alias message state
        (RawOpenAIMessage), so callers must treat it as read-only."""
        ...

    @abstractmethod
    def to_json(self) -> dict:
        ...


@dataclass
class RawOpenAIMessage(AbstractMessage):
    """Assistant and tool messages, kept as raw OpenAI message params."""
    raw: chat.chat_completion_message_param.ChatCompletionMessageParam

    @property
    def role(self) -> str:
        return self.raw.get("role", "unknown")

    def completion_param(self) -> chat.chat_completion_message_param.ChatCompletionMessageParam:
        return self.raw

    def to_json(self) -> dict:
        return {"kind": "raw", "raw": self.raw}


COMPACTED_SYSTEM_PROMPT = """\
You are an assistant having a conversation with a user. Earlier conversation history has been compacted into the summary below:

{summary}

---
Context management notes:
- The most recent user message is always preserved verbatim and is authoritative for the current task. Messages around it may change: earlier turns are replaced by the summary above, and older tool results after it may be trimmed. 
- This conversation will be compacted again when the context limit is reached. Persist important state rather than keeping it only in the context window.
"""


@dataclass
class SystemPrompt(AbstractMessage):
    content: str
    is_compressed: bool = False

    @property
    def role(self) -> str:
        return "system"

    def completion_param(self) -> chat.chat_completion_system_message_param.ChatCompletionSystemMessageParam:
        if self.is_compressed:
            content = COMPACTED_SYSTEM_PROMPT.format(summary=self.content)
        else:
            content = self.content
        return {
            'role': 'system',
            'content': content
        }

    def to_json(self) -> dict:
        return {"kind": "system", "content": self.content, "is_compressed": self.is_compressed}


RESPONSE_SCHEMA_PROMPT = """

---
Please respond in JSON format without any additional text. 
The JSON should conform to the following schema:
{schema}
"""

@dataclass
class UserMessage(AbstractMessage):
    text: str
    images: list[str] = field(default_factory=list)
    """Normalized URLs (data or http(s)) from image_to_url."""
    response_schema: str | None = None
    """JSON schema; appended via RESPONSE_SCHEMA_PROMPT at completion_param time."""

    @property
    def role(self) -> str:
        return "user"

    def completion_param(self) -> chat.chat_completion_user_message_param.ChatCompletionUserMessageParam:
        text = self.text
        if self.response_schema is not None:
            text += RESPONSE_SCHEMA_PROMPT.format(schema=self.response_schema)
        if not self.images:
            return {"role": "user", "content": text}
        parts: list[chat.chat_completion_content_part_param.ChatCompletionContentPartParam] = []
        if text:
            parts.append({"type": "text", "text": text})
        parts.extend({"type": "image_url", "image_url": {"url": url}} for url in self.images)
        return {"role": "user", "content": parts}

    def to_json(self) -> dict:
        return {"kind": "user", "text": self.text, "images": self.images, "response_schema": self.response_schema}


def message_from_json(data: dict) -> AbstractMessage:
    kind = data.get("kind", "raw")
    if kind == "system":
        return SystemPrompt(content=data["content"], is_compressed=data.get("is_compressed", False))
    if kind == "user":
        return UserMessage(
            text=data.get("text", ""),
            images=list(data.get("images", [])),
            response_schema=data.get("response_schema"),
        )
    return RawOpenAIMessage(raw=data["raw"])
