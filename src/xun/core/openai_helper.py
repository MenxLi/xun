from typing import Any, Optional
from pydantic import PrivateAttr, SerializerFunctionWrapHandler, model_serializer
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall
from openai.types.chat.chat_completion_message import ChatCompletionMessage

class ChatCompletionMessageWithReasoning(ChatCompletionMessage):
    """
    My hack to add reasoning field to the model...

    By default the OpenAI API does not support reasoning field, 
    but some models require `preserve_thinking` to be set to true, 
    and need the reasoning field to be present in the request...

    To validate this works:
    https://www.reddit.com/r/LocalLLaMA/comments/1sne4gh/psa_qwen36_ships_with_preserve_thinking_make_sure/

    Note the field name may be different for different providers, 
    `reasoning` is used by vllm.
    """
    reasoning: Optional[str]
    _reasoning_field: str = PrivateAttr(default="reasoning")

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Serialize `reasoning` under the provider-specific key in `_reasoning_field`."""
        data = handler(self)
        if data.get("reasoning") is None:
            data.pop("reasoning")
            return data
        if self._reasoning_field != "reasoning" and "reasoning" in data:
            data[self._reasoning_field] = data.pop("reasoning")
        return data


def accumulate_tool_calls(
    deltas: list[ChoiceDeltaToolCall],
 ) -> list[ChatCompletionMessageToolCall]:
    """Accumulate streamed tool-call deltas into complete tool calls."""

    calls: dict[int, dict] = {}

    for delta in deltas:
        index = delta.index

        call = calls.setdefault(
            index,
            {
                "id": None,
                "type": "function",
                "function": {
                    "name": "",
                    "arguments": "",
                },
            },
        )

        if delta.id is not None:
            call["id"] = delta.id

        if delta.type is not None:
            call["type"] = delta.type
            assert call["type"] == "function", f"Unsupported tool call type: {call['type']}"

        if delta.function is not None:
            if delta.function.name is not None:
                call["function"]["name"] += delta.function.name

            if delta.function.arguments is not None:
                call["function"]["arguments"] += delta.function.arguments

    return [
        ChatCompletionMessageToolCall.model_validate(call)
        for _, call in sorted(calls.items())
    ]