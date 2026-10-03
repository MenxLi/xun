from __future__ import annotations
import uuid
from typing import Any

import json, time
import json_repair
from pydantic import BaseModel

from .display_abstract import ErrorEvent, ModelMessageEvent, ModelWorkingEvent, ToolCallEvent, ToolResultEvent
from .error_catch import ErrorInfo, Result
from .openai_helper import accumulate_tool_calls, ChatCompletionMessageWithReasoning
from .types import CancelledError, ToolResultType
from .hooks import HookArgs

# rename for semantics
ExecutionLoopParams = HookArgs.BeforeExecutionArgs

COMPLETION_RETRY_BACKOFF = 0.5
COMPLETION_MAX_RETRIES = 3
USER_RETRY_GRANT = 3

def execution_loop(params: ExecutionLoopParams) -> str | BaseModel:
    # cancellation is the caller's contract: Agent.execute wraps this loop in cancellable_execution
    agent = params.agent

    agent.hooks.before_execution.invoke(params)

    if params.takeover_result is not None:
        # skip the loop entirely, before the response schema is attached to the conversation
        result = params.takeover_result
    else:
        result = ""
        finished = False
        if params.schema is not None:
            # prompt-injected schema
            agent.conversation.set_response_schema(params.schema)
        for iteration in range(params.max_iterations):
            agent.check_cancel()
            agent.hooks.before_execution_step.invoke(HookArgs.BeforeExecutionStepArgs(agent=agent))
            model_call_id = str(uuid.uuid4())
            agent.display_event(ModelWorkingEvent(
                model_call_id=model_call_id,
                remaining_iterations=params.max_iterations - iteration
                ))
            should_continue, result = _execute_step(params, model_call_id)
            if not should_continue:
                finished = True
                break

        if not finished:
            agent.display_event(ErrorEvent(message="Maximum tool call iterations exceeded."))
            raise RuntimeError("Maximum tool call iterations exceeded.")

    if params.schema is not None:
        try:
            res_object = json_repair.loads(result)
            return params.schema.model_validate(res_object)
        except Exception as e:
            agent.display_event(ErrorEvent(message=f"Failed to parse result into {params.schema}: {e}"))
            raise e
    return result

def _execute_step(params: ExecutionLoopParams, call_id: str) -> tuple[bool, str]:

    agent = params.agent

    completion_retry_backoff = COMPLETION_RETRY_BACKOFF
    n_completion_max_retries = COMPLETION_MAX_RETRIES

    while True:
        agent.check_cancel()
        try:
            config = agent.config
            model_params = {
                "model": config.model.name,
                "messages": agent.conversation.completion_params(),
            }
            tools_json = agent.toolbox.list_tools_json(config.model.capabilities)
            if tools_json:
                model_params["tools"] = tools_json
                model_params["tool_choice"] = "auto"
            if config.model.temperature is not None:
                model_params["temperature"] = config.model.temperature
            if config.model.reasoning_effort is not None:
                model_params["reasoning_effort"] = config.model.reasoning_effort
            if params.schema is not None and not tools_json:
                # No tools to protect: 
                # let the provider enforce on top of prompt-injected schema
                # With tools registered, response_format may suppress tool calls
                model_params["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": params.schema.__name__,
                        "schema": params.schema.model_json_schema(),
                        "strict": False,
                    },
                }

            content_accumulator = ""
            reasoning_accumulator = ""
            tool_calls_accumulator = []
            usage = None

            with agent.api_call_semaphore:
                stream = agent.openai_client.chat.completions.create(
                    stream=True,
                    timeout=900,        # TTFT can be slow
                    stream_options={
                        "include_usage": True,
                    },
                    **model_params
                    )

                def get_reasoning_delta(delta):
                    if agent.config.model.reasoning_field:
                        return getattr(delta, agent.config.model.reasoning_field, None)

                    if (reasoning:=getattr(delta, "reasoning", None)):
                        agent.config.model.reasoning_field = "reasoning"
                        return reasoning
                    elif (reasoning:=getattr(delta, "reasoning_content", None)):
                        agent.config.model.reasoning_field = "reasoning_content"
                        return reasoning
                    else:
                        return None

                for chunk in stream:
                    agent.check_cancel()

                    if len(chunk.choices) > 0:
                        delta = chunk.choices[0].delta

                        if (content_delta := delta.content):
                            hook_args = HookArgs.TextDelta(
                                agent=agent,
                                model_call_id=call_id,
                                content=content_delta
                            )
                            agent.hooks.model_text_delta.invoke(hook_args)
                            content_accumulator += hook_args.content

                        if (reasoning_delta := get_reasoning_delta(delta)):
                            hook_args = HookArgs.TextDelta(
                                agent=agent,
                                model_call_id=call_id,
                                content=reasoning_delta
                            )
                            agent.hooks.model_reasoning_delta.invoke(hook_args)
                            reasoning_accumulator += hook_args.content

                        if (tool_calls_delta := getattr(delta, "tool_calls", None)):
                            for tool_call in tool_calls_delta:
                                tool_calls_accumulator.append(tool_call)

                    if chunk.usage:
                        usage = chunk.usage

                message = ChatCompletionMessageWithReasoning(
                    role="assistant",
                    content=content_accumulator,
                    tool_calls=accumulate_tool_calls(tool_calls_accumulator) if len(tool_calls_accumulator) > 0 else None,   # type: ignore
                    reasoning=reasoning_accumulator if reasoning_accumulator else None,
                )
                if agent.config.model.reasoning_field:
                    message._reasoning_field = agent.config.model.reasoning_field

                if not message.content and not message.tool_calls:
                    # occasionally providers stream a usage-only chunk and an empty message;
                    # treat as a failed turn so it goes through the retry path below
                    # instead of ending the run with "" or polluting the history
                    raise RuntimeError("Model returned an empty response")

            break

        except (CancelledError, KeyboardInterrupt):
            raise

        except Exception as e:
            if n_completion_max_retries > 0:
                agent.warning(f"Error during chat completion: {e}, will retry in {completion_retry_backoff}s")
                time.sleep(completion_retry_backoff)
                n_completion_max_retries -= 1
                completion_retry_backoff *= 2
                continue
            else:
                agent.error(message=f"Error during chat completion: {e}.")
                try:
                    approved = agent.get_confirm("Retry?", _skip_auto_confirm=True).choice
                except NotImplementedError:
                    approved = False    # headless display has no human to ask
                if not approved:
                    raise e
                n_completion_max_retries = USER_RETRY_GRANT
                completion_retry_backoff = COMPLETION_RETRY_BACKOFF
                continue

    if usage:
        agent.conversation.total_tokens = usage.total_tokens
    if message.content:
        total_tokens = agent.conversation.total_tokens
        if total_tokens is None:
            # all openai-compatible providers should report token usage upon here
            # so should not happen, but just in case
            raise RuntimeError("Model provider did not report token usage")
        agent.display_event(ModelMessageEvent(
            model_call_id=call_id,
            content=message.content,
            reasoning=message.reasoning,
            total_tokens=total_tokens,
            ))
    tool_called = False

    tool_results: list[tuple[str, ToolResultType]] = []
    if message.tool_calls:
        tool_calls = [tool_call for tool_call in message.tool_calls if tool_call.type == "function"]

        agent.hooks.before_tool_call.invoke(HookArgs.BeforeToolCallArgs(
            agent=agent,
            tool_calls=tool_calls
        ))

        for tool_call in tool_calls:
            agent.check_cancel()
            tool_id = tool_call.id
            tool_name = tool_call.function.name
            arguments = tool_call.function.arguments

            tool_res: ToolResultType
            try:
                # fix potential JSON issues in the tool arguments
                arguments_json: Any = json_repair.loads(arguments)
                if not arguments_json: # None, "", [], {}
                    arguments_json = {}
                # keep the assistant history consistent with the repaired
                tool_call.function.arguments = json.dumps(arguments_json, ensure_ascii=False)

                agent.display_event(ToolCallEvent(tool_call_id=tool_id, tool_name=tool_name, args=arguments_json))
                tool_res = agent.toolbox.call_tool(
                    agent=agent,
                    tool_name = tool_name,
                    arguments = arguments_json,
                    context = params.context_value
                    )
                if tool_res.is_ok():
                    agent.display_event(ToolResultEvent(tool_call_id=tool_id, result=tool_res.value_json()))
                else:
                    agent.warning(f"Tool {tool_name} failed: {tool_res.unwrap_err().error}")
            except CancelledError:
                raise
            except Exception as e:
                agent.error(f"Tool pipeline {tool_name} failed: {e}")
                tool_res = Result.Err(ErrorInfo(error="Tool pipeline failed", details=str(e)))

            tool_results.append((tool_id, tool_res))
            tool_called = True

        agent.hooks.after_tool_call.invoke(HookArgs.AfterToolCallArgs(
            agent=agent,
            tool_results=tool_results
        ))

    # conversation update
    agent.conversation.add_agent_message(message)
    for tool_id, tr in tool_results:
        agent.conversation.add_tool_result(tool_id, tr)

    agent.hooks.after_execution_step.invoke(HookArgs.AfterExecutionStepArgs(
        agent=agent,
    ))

    return tool_called, message.content or ""
