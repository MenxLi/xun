from __future__ import annotations
from openai.types import chat
from pydantic import BaseModel
from typing import Any, Callable, Sequence, cast
from typing_extensions import TypedDict
from pathlib import Path
import uuid, json, time
from PIL.Image import Image
from .compact import CompactionCounter, SummaryCompactResult, ToolCallCompactResult
from .conversation_message import (
    AbstractMessage, COMPACTION_NOTICE, RawOpenAIMessage, SystemPrompt, UserMessage, ToolResultMessage,
    message_from_json, remove_empty_tool_calls,
)
from .types import Result, ToolResultType
from .util import image_to_url
from .openai_helper import ChatCompletionMessageWithReasoning


MAX_HISTORY_CONTENT_LENGTH = 1000
MAX_COMPACTED_TOOLCALL_RECORDS = 2000


class Conversation:
    class MessageRecord(TypedDict):
        role: str
        content: str

    def __init__(self):
        self.messages: list[AbstractMessage] = []
        self.conversation_id: str = uuid.uuid4().hex

        # refreshed after each model call, stale once the history is edited
        self.total_tokens: int | None = None     

        self._compacted_toolcalls: dict[str, str] = {}
        self.compaction_counter = CompactionCounter()
    
    def clear(self):
        """ Clear messages, keeping the leading system message if present. """
        if self.messages and isinstance(self.messages[0], SystemPrompt):
            del self.messages[1:]
        else:
            self.messages.clear()
        self.total_tokens = None
        self._compacted_toolcalls.clear()
        self.compaction_counter = CompactionCounter()

    def to_json(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "tokens_used": self.total_tokens,
            "messages": [msg.to_json() for msg in self.messages],
            "compacted_toolcalls": self._compacted_toolcalls,
            "compaction": self.compaction_counter.to_json(),
        }
    
    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False)
    
    def dump(self, file_path: str | Path):
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(self.dumps())
    
    def load_json(self, data: dict):
        self.conversation_id = data.get("conversation_id", self.conversation_id)
        self.messages = [message_from_json(msg) for msg in data.get("messages", [])]
        # "tokens_used" is the on-disk key, kept stable for previously saved conversations
        self.total_tokens = data.get("tokens_used", None)
        self._compacted_toolcalls = dict(data.get("compacted_toolcalls", {}))
        self.compaction_counter = CompactionCounter.from_json(data.get("compaction", {}))
    
    def loads(self, data: str):
        obj = json.loads(data)
        self.load_json(obj)
    
    def load(self, file_path: str | Path):
        with open(file_path, "r", encoding="utf-8") as f:
            self.loads(f.read())
    
    def set_system_message_content(self, content: str, is_compressed: bool | None = None):
        if self.messages and isinstance(self.messages[0], SystemPrompt):
            prompt = self.messages[0]
        else:
            prompt = SystemPrompt(content="")
            self.messages.insert(0, prompt)
        prompt.content = content
        if is_compressed is not None:
            prompt.is_compressed = is_compressed
    
    def set_system_persistent_section(self, name: str, content: str) -> None:
        if self.messages and isinstance(self.messages[0], SystemPrompt):
            sections = self.messages[0].persist_sections
        elif content:
            prompt = SystemPrompt(content="")
            self.messages.insert(0, prompt)
            sections = prompt.persist_sections
        else:
            return
        if content:
            sections[name] = content
        else:
            sections.pop(name, None)

    def completion_params(self) -> list[chat.chat_completion_message_param.ChatCompletionMessageParam]:
        return [msg.completion_param() for msg in self.messages]

    @staticmethod
    def content_to_text(content: Any, truncate: bool = False) -> str:
        if isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, indent=4)

        if truncate and len(text) > MAX_HISTORY_CONTENT_LENGTH:
            return text[:MAX_HISTORY_CONTENT_LENGTH] + "...(truncated)"
        return text

    def set_response_schema(self, schema: type[BaseModel]) -> None:
        """Require the trailing user message to be answered as JSON matching `schema`."""
        if not self.messages or not isinstance((last_msg:=self.messages[-1]), UserMessage):
            raise ValueError("No user message to attach a response schema to. Please add a user message first.")
        last_msg.response_schema = json.dumps(schema.model_json_schema())

    def add_user_message(
        self,
        content: str,
        images: Sequence[str | Image] | None = None,
        is_synthetic: bool = False,
    ) -> None:
        normalized_images = [image_to_url(image) for image in images or ()]
        self.messages.append(UserMessage(text=content, images=normalized_images, is_synthetic=is_synthetic))
    
    def add_developer_message(self, content: str) -> None:
        dev_msg: chat.chat_completion_message_param.ChatCompletionMessageParam = {"role": "developer", "content": content}
        self.messages.append(RawOpenAIMessage(raw=dev_msg))
    
    def add_agent_message(self, msg: chat.chat_completion_message.ChatCompletionMessage | ChatCompletionMessageWithReasoning):
        self.messages.append(RawOpenAIMessage(raw=remove_empty_tool_calls(msg.model_dump())))
    
    def add_tool_result(self, tool_call_id: str, content: ToolResultType):
        """The tool call itself is recorded by the preceding assistant message."""
        self.messages.append(ToolResultMessage(tool_call_id=tool_call_id, content=content))

    def pop_last_message_if_user(self) -> UserMessage | None:
        if not self.messages or not isinstance(self.messages[-1], UserMessage):
            return None
        return cast(UserMessage, self.messages.pop())
    
    def pop_from_last_user_message(self, inclusive: bool = True) -> list[AbstractMessage]:
        """Pop everything after the last user message; `inclusive` also pops the message itself."""
        for i in range(len(self.messages)-1, -1, -1):
            if isinstance(self.messages[i], UserMessage):
                keep = i if inclusive else i + 1
                popped = self.messages[keep:]
                self.messages = self.messages[:keep]
                return popped
        return []
    
    @classmethod
    def _estimate_message_length(cls, message: AbstractMessage | list[AbstractMessage] | dict) -> int:
        """Rough text-length proxy for a message's token contribution."""
        length_kw = ['content', 'text', 'reasoning', 'reasoning_content']
        total_length = 0
        if isinstance(message, list):
            return sum(cls._estimate_message_length(item) for item in message if isinstance(item, (AbstractMessage, dict)))
        msg = message.completion_param() if isinstance(message, AbstractMessage) else message
        for k in msg:
            if isinstance(msg[k], str) and k in length_kw:
                total_length += len(msg[k])
            elif isinstance(msg[k], dict):
                total_length += cls._estimate_message_length(msg[k])
            elif isinstance(msg[k], list):
                total_length += sum(cls._estimate_message_length(item) for item in msg[k] if isinstance(item, (AbstractMessage, dict)))
        return total_length
    
    def estimated_message_length(self) -> int:
        return self._estimate_message_length(self.messages)
    
    def compact_toolcall(self, keep_max: int = 12) -> ToolCallCompactResult:
        """Replace all but the most recent `keep_max` tool results with placeholders,
        keeping the originals retrievable by tool call id."""
        self.compaction_counter.tool_rounds += 1
        n_compacted = 0
        message_length_before: int = self.estimated_message_length()
        for i in range(len(self.messages) - 1, -1, -1):
            message = self.messages[i]
            if not isinstance(message, ToolResultMessage):
                continue
            keep_max -= 1
            if keep_max < 0:
                old_content = message.content_str
                new_content = f"[Compacted, ID: {message.tool_call_id}. If this content is still needed, call extract_compacted_tool_result with this ID or re-run the tool.]"
                if len(old_content) > len(new_content):
                    message.content = Result.Ok(new_content)
                    self._compacted_toolcalls[message.tool_call_id] = old_content

                    # set high enough that recalling a trimmed tool call is nearly impossible
                    if len(self._compacted_toolcalls) > MAX_COMPACTED_TOOLCALL_RECORDS:
                        keys_to_remove = list(self._compacted_toolcalls.keys())[:len(self._compacted_toolcalls) // 2]
                        for key in keys_to_remove:
                            del self._compacted_toolcalls[key]

                    n_compacted += 1
        message_length_after: int = self.estimated_message_length()
        return ToolCallCompactResult(
            reclaimed_count=n_compacted,
            reclaimed_fraction=((message_length_before - message_length_after) / message_length_before) if message_length_before > 0 else 0.0,
        )
    
    def compacted_toolcall_result(self, toolcall_id: str) -> str | None:
        """Original content of a compacted tool call, None if it was never compacted."""
        return self._compacted_toolcalls.get(toolcall_id)

    def compact(self, summarize: Callable[[list[AbstractMessage]], str | None], keep_recent: int):
        """Replace messages before a cut point with a `summarize(messages)` summary
        (None to abort) in the system message, keeping a tail of at most `keep_recent`
        messages. History is untouched unless the status is COMPACTED.

        The anchor — the last non-synthetic user message — is always kept verbatim:
        cut at it when its tail fits, otherwise at the window start advanced to a
        self-contained turn (`user`/`assistant`) so the tail never opens on an
        orphaned tool result or a context-bound annotation. When carrying the anchor
        across that cut left a gap, a `developer` notice right after it marks it.
        """
        len_before = self.estimated_message_length()
        msgs = self.messages

        anchor: int = -1    # the place of the last non-synthetic user message
        for i in range(len(msgs) - 1, -1, -1):
            if isinstance(msg_i:=msgs[i], UserMessage) and not msg_i.is_synthetic:
                anchor = i
                break
        assert anchor != -1, "There must be at least one non-synthetic user message in the conversation."

        # cut at the anchor when its tail fits (max picks the anchor), otherwise at the
        # window start; when the window start lands inside the anchor's tail (anchor > 0),
        # the anchor is carried to the head of the tail below
        cut = max(anchor, len(msgs) - keep_recent)
        while cut < len(msgs) and msgs[cut].role not in ("user", "assistant"):
            cut += 1

        condense_messages = msgs[:cut]
        keep_messages = ([msgs[anchor]] if anchor < cut else []) + msgs[cut:]

        Status = SummaryCompactResult.Status
        if not any(m.role != "system" for m in condense_messages):
            return SummaryCompactResult(
                Status.NOTHING_TO_CONDENSE, 
                "Nothing to condense in conversation history."
                )

        summary = summarize(condense_messages)
        if summary is None:
            return SummaryCompactResult(
                Status.SUMMARIZE_FAILED, 
                "Conversation compaction did not produce a summary."
                )

        if anchor + 1 < cut:
            # the carried anchor lost msgs[anchor+1:cut] behind it
            notice: chat.chat_completion_message_param.ChatCompletionMessageParam = {"role": "developer", "content": COMPACTION_NOTICE}
            keep_messages.insert(1, RawOpenAIMessage(raw=notice))

        self.set_system_message_content(summary, is_compressed=True)
        self.messages = self.messages[:1] + keep_messages
        # the stale count refers to pre-compaction history and would re-trigger auto-compaction
        self.total_tokens = None
        self.compaction_counter = CompactionCounter(summary_rounds=self.compaction_counter.summary_rounds + 1)
        return SummaryCompactResult(
            Status.COMPACTED, 
            "Conversation history condensed.", 
            reclaimed_fraction = 1 - self.estimated_message_length() / len_before if len_before else 0.0
            )
    
    def to_history(self, truncate = False) -> list[MessageRecord]:
        """For display history event, may change in the future."""
        res = []
        for msg in self.messages:
            param = msg.completion_param()
            res.append(self.MessageRecord(
                role=msg.role,
                content=self.content_to_text(param.get("content", ""), truncate=truncate),
            ))
        return res
