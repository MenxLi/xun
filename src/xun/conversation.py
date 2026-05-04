from openai.types import chat
from typing import Any, cast
from typing_extensions import TypedDict
from pathlib import Path
from urllib.parse import urlparse
import base64, mimetypes
import uuid, json, time


MAX_HISTORY_CONTENT_LENGTH = 1000


def _image_to_url(image: str) -> str:
    parsed = urlparse(image)
    if parsed.scheme in {"http", "https", "data"}:
        return image

    image_path = Path(image).expanduser()
    if not image_path.exists() or not image_path.is_file():
        raise ValueError(f"Image file not found: {image}")

    mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    try:
        image_bytes = image_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Failed to read image file {image}: {exc}") from exc

    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


class Conversation:
    class MessageRecord(TypedDict):
        role: str
        content: str

    def __init__(self):
        self.messages: list[chat.chat_completion_message_param.ChatCompletionMessageParam] = []
        self.conversation_id: str = uuid.uuid4().hex
    
    def clear(self):
        self.messages.clear()
    
    def dump(self, file_path: str | Path):
        with open(file_path, "w") as f:
            json.dump({
                "conversation_id": self.conversation_id,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "messages": self.messages,
            }, f, indent=2)
    
    def load(self, file_path: str | Path):
        with open(file_path, "r") as f:
            data = json.load(f)
            self.conversation_id = data.get("conversation_id", self.conversation_id)
            self.messages = data.get("messages", [])
    
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

    def add_user_message(self, content: str, images: list[str] | None = None):
        user_content: str | list[dict[str, Any]]
        if not images:
            user_content = content
        else:
            parts: list[dict[str, Any]] = []
            if content:
                parts.append({"type": "text", "text": content})
            parts.extend({
                "type": "image_url", 
                "image_url": {"url": _image_to_url(image)}
                } for image in images)
            user_content = parts

        self.messages.append(cast(chat.ChatCompletionUserMessageParam, {"role": "user", "content": user_content}))
    
    def add_agent_message(self, msg: chat.chat_completion_message.ChatCompletionMessage):
        self.messages.append(msg.to_dict())     # type: ignore
    
    def add_tool_call(self, tool_call_id: str, content: str):
        """ Add tool call result, the tool call is recorded via assistant message """
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
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
        
