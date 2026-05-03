from openai.types import chat
from typing import Any
from typing_extensions import TypedDict
from pathlib import Path
import uuid, json, time

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
    
    def add_user_instruct(self, content: str):
        self.messages.append({"role": "user", "content": content})
    
    def add_agent_message(self, msg: chat.chat_completion_message.ChatCompletionMessage):
        self.messages.append(msg.to_dict())     # type: ignore
    
    def add_tool_call(self, tool_call_id: str, content: str):
        """ Add tool call result, the tool call is recorded via assistant message """
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })
    
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
            if isinstance(content, dict):
                content = json.dumps(content, indent=4)
            if isinstance(content, str) and truncate and len(content) > 1000:
                content = content[:1000] + "...(truncated)"
            res.append(self.MessageRecord(
                role=role,
                content=str(content),
            ))
        return res
        
