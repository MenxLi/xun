from __future__ import annotations
from openai.types import chat
from typing import Any, Callable, Sequence, cast
from typing_extensions import TypedDict
from pathlib import Path
import uuid, json, time
from PIL.Image import Image
import jinja2
import markdown
from markupsafe import Markup, escape
from .config import ASSET_DIR
from .toolbox import ToolResultType
from .util import image_to_url
from .openai_helper import ChatCompletionMessageWithReasoning


MAX_HISTORY_CONTENT_LENGTH = 1000
DEFAULT_KEEP_RECENT = 24
def _remove_empty_tool_calls(message: Any) -> Any:
    # some provider does not allow empty list for tool_calls
    if not isinstance(message, dict):
        return message

    sanitized = dict(message)
    if sanitized.get("tool_calls") == []:
        sanitized.pop("tool_calls", None)
    return sanitized


def _expand_json_content(content: Any) -> Any:
    if not isinstance(content, str):
        return content

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content

    return parsed if isinstance(parsed, (dict, list)) else content

class Conversation:
    class MessageRecord(TypedDict):
        role: str
        content: str

    def __init__(self):
        self.messages: list[chat.chat_completion_message_param.ChatCompletionMessageParam] = []
        self.conversation_id: str = uuid.uuid4().hex

        # will update after each model call, 
        # but not guaranteed to be accurate if user edits the conversation
        self.total_tokens: int | None = None     

        self._compacted_toolcalls: dict[str, str] = {}
    
    def clear(self):
        """ Clear messages, keeping the leading system message if present. """
        if self.messages and self.messages[0].get("role") == "system":
            del self.messages[1:]
        else:
            self.messages.clear()
        self.total_tokens = None
        self._compacted_toolcalls.clear()

    def to_json(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "tokens_used": self.total_tokens,
            "messages": self.messages,
            "compacted_toolcalls": self._compacted_toolcalls,
        }
    
    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False)
    
    def dump(self, file_path: str | Path):
        with open(file_path, "w") as f:
            return f.write(self.dumps())
    
    def load_json(self, data: dict):
        self.conversation_id = data.get("conversation_id", self.conversation_id)
        self.messages = [_remove_empty_tool_calls(msg) for msg in data.get("messages", [])]
        # "tokens_used" is the on-disk key, kept stable for previously saved conversations
        self.total_tokens = data.get("tokens_used", None)
        # conversations saved before compaction existed have no originals to restore
        self._compacted_toolcalls = dict(data.get("compacted_toolcalls", {}))
    
    def loads(self, data: str):
        obj = json.loads(data)
        self.load_json(obj)
    
    def load(self, file_path: str | Path):
        with open(file_path, "r") as f:
            self.loads(f.read())
    
    def set_system_message_content(self, content: str):
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = content
        else:
            self.messages.insert(0, {"role": "system", "content": content})

    @staticmethod
    def content_to_text(content: Any, truncate: bool = False) -> str:
        if isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, indent=4)

        if truncate and len(text) > MAX_HISTORY_CONTENT_LENGTH:
            return text[:MAX_HISTORY_CONTENT_LENGTH] + "...(truncated)"
        return text

    @classmethod
    def content_to_html(cls, content: Any) -> Markup:
        def render_text(text: str) -> Markup:
            return Markup(markdown.markdown(
                str(escape(text)),
                extensions=["fenced_code", "tables"],
            ))

        if not isinstance(content, list):
            return render_text(cls.content_to_text(content))

        parts: list[Markup] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(render_text(cls.content_to_text(item)))
                continue

            if item.get("type") == "text":
                parts.append(render_text(str(item.get("text", ""))))
                continue

            image_url = item.get("image_url", {}).get("url") if isinstance(item.get("image_url"), dict) else None
            if item.get("type") == "image_url" and isinstance(image_url, str):
                parts.append(Markup(
                    '<figure class="message-image"><img src="{}" alt="User-provided image" loading="lazy"></figure>'
                ).format(escape(image_url)))
                continue

            parts.append(render_text(cls.content_to_text(item)))

        return Markup("\n").join(parts)
    
    def append_user_message(self, extra_content: str ):
        if not self.messages or self.messages[-1].get("role") != "user":
            raise ValueError("No user message to append to. Please add a user message first.")

        last_message = self.messages[-1]
        last_content = last_message.get("content", "")
        if isinstance(last_content, str):
            last_message["content"] = last_content + extra_content
        elif isinstance(last_content, list):
            for item in last_content:
                if isinstance(item, dict) and item.get("type") == "text":
                    assert "text" in item, "Text content missing in user message part."
                    item["text"] = item.get("text", "") + extra_content
                    break
        else:
            raise ValueError(f"Unexpected content type in last user message: {type(last_content)}")

    def add_user_message(
        self,
        content: str,
        images: Sequence[str | Image] | None = None,
    ) -> None:
        normalized_images = [image_to_url(image) for image in images or ()]
        user_content: str | list[dict[str, Any]]
        if not normalized_images:
            user_content = content
        else:
            parts: list[dict[str, Any]] = []
            if content:
                parts.append({"type": "text", "text": content})
            parts.extend({
                "type": "image_url", 
                "image_url": {"url": image}
                } for image in normalized_images)
            user_content = parts

        self.messages.append(cast(chat.ChatCompletionUserMessageParam, {"role": "user", "content": user_content}))
    
    def add_agent_message(self, msg: chat.chat_completion_message.ChatCompletionMessage | ChatCompletionMessageWithReasoning):
        self.messages.append(_remove_empty_tool_calls(msg.model_dump()))     # type: ignore
    
    def add_tool_result(self, tool_call_id: str, content: ToolResultType):
        """ Add tool call result, the tool call is recorded via assistant message """
        try:
            content_str = content.value_str()
        except Exception as e:
            content_str = f"[Error] Failed to serialize tool result: {str(e)}"
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content_str
        })

    def pop_last_message_if_user(self) -> dict[str, Any] | None:
        if not self.messages or self.messages[-1].get("role") != "user":
            return None
        return cast(dict[str, Any], self.messages.pop())
    
    def pop_from_last_user_message(self, inclusive: bool = True) -> list[Any]:
        """
        inclusive=True: pop the last user message as well as afterwards
        inclusive=False: keep the last user message, pop afterwards
        """
        for i in range(len(self.messages)-1, -1, -1):
            if self.messages[i]["role"] == "user":
                old = self.messages
                if inclusive:
                    self.messages = self.messages[:i]
                    return old[i:]
                else:
                    if i == len(self.messages) - 1:
                        return []
                    self.messages = self.messages[:i+1]
                    return old[i+1:]
        return []
    
    def compact_toolcall(self, keep_max: int = 16) -> int:
        """
        Condense the tool call history by marking older tool calls as compacted, keeping only the most recent `keep_max` tool calls.
        Returns the number of tool call results newly compacted.
        """
        compacted = 0
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "tool":
                keep_max -= 1
                msg: chat.chat_completion_tool_message_param.ChatCompletionToolMessageParam = self.messages[i] # type: ignore
                assert 'tool_call_id' in msg
                assert 'content' in msg
                if keep_max < 0:
                    toolcall_id = msg["tool_call_id"]
                    old_content = msg["content"]
                    new_content = f"[Compacted, ID: {toolcall_id}]"
                    assert isinstance(old_content, str)
                    if len(old_content) > len(new_content):
                        self.messages[i]['content'] = new_content
                        self._compacted_toolcalls[toolcall_id] = old_content
                        compacted += 1
        return compacted
    
    def compacted_toolcall_result(self, toolcall_id: str) -> str | None:
        """
        Retrieve the original content of a compacted tool call by its ID.
        Returns None if the tool call is not compacted or does not exist.
        """
        return self._compacted_toolcalls.get(toolcall_id)

    def compact(self, summarize: Callable[[list[Any]], str | None], keep_recent: int = DEFAULT_KEEP_RECENT) -> bool:
        """
        Replace older messages with a system-message summary from `summarize(messages)`
        (None to abort), keeping a bounded recent tail.

        Cut at the last user message when its tail fits in `keep_recent`, else keep only
        the most recent `keep_recent` messages, advancing off `tool` messages so the tail
        never starts with orphaned tool results.

        Returns False, leaving history untouched, when there is nothing beyond the system
        message to condense or `summarize` returns None.
        """
        msgs = self.messages

        cut: int | None = None
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                if len(msgs) - i <= keep_recent:
                    cut = i
                break
        if cut is None:
            cut = max(len(msgs) - keep_recent, 0)
            while cut < len(msgs) and msgs[cut].get("role") == "tool":
                cut += 1

        condense_messages = msgs[:cut]
        keep_messages = msgs[cut:]

        if not any(m.get("role") != "system" for m in condense_messages):
            return False

        summary = summarize(condense_messages)
        if summary is None:
            return False

        sys_msg = f"You are an assistant having a conversation with a user. Here is the summary of the conversation history so far:\n{summary}"
        self.set_system_message_content(sys_msg)  # in-place on the leading system message, or inserted at index 0
        self.messages = self.messages[:1] + keep_messages
        return True
    
    def to_history(self, truncate = False) -> list[MessageRecord]:
        res = []
        for msg in self.messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            res.append(self.MessageRecord(
                role=role,
                content=self.content_to_text(content, truncate=truncate),
            ))
        return res

    def render_history_as_html(self) -> str:
        """ Render the conversation as a standalone HTML page.

        Messages are grouped into render blocks: system prompts, user/assistant
        messages, and collapsible activity blocks where each tool call is
        paired with its result (matched by tool_call_id).
        """
        blocks: list[dict[str, Any]] = []
        message_number = 0
        rows_by_call_id: dict[str, dict[str, Any]] = {}
        activity: dict[str, Any] | None = None

        def current_activity() -> dict[str, Any]:
            nonlocal activity
            if activity is None:
                activity = {"kind": "activity", "tools": []}
                blocks.append(activity)
            return activity

        for message in self.messages:
            role = message.get("role", "unknown")

            if role == "system":
                blocks.append({
                    "kind": "system",
                    "content": self.content_to_html(message.get("content", "")),
                })
                continue

            if role == "tool":
                tool_call_id = message.get("tool_call_id")
                row = rows_by_call_id.get(tool_call_id) if tool_call_id else None
                if row is None:
                    row = {"name": "Tool result", "args": None, "result": None}
                    current_activity()["tools"].append(row)
                    if tool_call_id:
                        rows_by_call_id[tool_call_id] = row
                row["result"] = _expand_json_content(message.get("content"))
                continue

            tool_calls = message.get("tool_calls")
            if tool_calls:
                content = message.get("content")
                if content not in (None, "", []):
                    blocks.append({
                        "kind": "message",
                        "role": role,
                        "content": self.content_to_html(content),
                        "message_id": None,
                        "message_hash": None,
                    })
                group = current_activity()
                for call in tool_calls:
                    function = call.get("function", {}) or {}
                    row = {
                        "name": function.get("name") or "Tool",
                        "args": _expand_json_content(function.get("arguments")),
                        "result": None,
                    }
                    group["tools"].append(row)
                    if call.get("id"):
                        rows_by_call_id[call["id"]] = row
                continue

            message_id = None
            message_hash = None
            if role in {"user", "assistant"}:
                message_number += 1
                message_id = f"message-{message_number}"
                message_hash = f"#{message_number}"

            blocks.append({
                "kind": "message",
                "role": role,
                "content": self.content_to_html(message.get("content", "")),
                "message_id": message_id,
                "message_hash": message_hash,
            })

        template_path = ASSET_DIR / "conversation.template.html"
        environment = jinja2.Environment(autoescape=True)
        environment.policies["json.dumps_kwargs"] = {"ensure_ascii": False}
        return environment.from_string(template_path.read_text(encoding="utf-8")).render(
            blocks=blocks,
            meta={"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), "total_tokens": self.total_tokens},
        )
