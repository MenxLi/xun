from dataclasses import dataclass
from typing import Optional, Callable, TYPE_CHECKING, cast
import concurrent.futures
from .types import CancelledError
from .toolcall import ToolCallContext
from .error_catch import ErrorInfo, except_safe, Result
if TYPE_CHECKING:
    from .agent import Agent

@dataclass
class AgentGetterParam:
    tool_context: ToolCallContext
    name: Optional[str] = None

AgentGetterProtocol = Callable[["AgentGetterParam"], "Agent[Agent.T.Uninit]"]

def agent_run_factory(agent_getter: AgentGetterProtocol):
    @except_safe
    def agent_run(ctx: ToolCallContext, task: str, name: Optional[str] = None) -> Result[str, ErrorInfo]:
        """
        Creates an isolated sub-agent to execute complex, multi-step tasks. 
        The new agent holds appropriate tools and capabilities to complete the assigned task, but starts with a blank context.

        Use this when:
        • The task is self-contained but requires multiple steps or heavy reasoning.
        • You need to isolate execution to avoid bloating the main conversation's context window.
        • The task does not require frequent back-and-forth with the parent agent.

        Input: A clear, self-contained instruction or tool directive specifying exactly what the new agent should do.
        Output: Returns the new agent's final output message upon successful completion, or an error message if the agent encounters any issues during execution.

        Notes:
        • The new agent starts with a blank context and cannot access the parent conversation history unless explicitly included in the instruction.
        • Prefer instructing the new agent to return results directly in its final message. File I/O can also be used for larger outputs or intermediate results when necessary, but should explicitly be mentioned in the instruction.
        """
        param = AgentGetterParam(tool_context = ctx, name = name)
        with agent_getter(param) as agent:
            # do not emit events to avoid cluttering the display
            try:
                return agent.instruct(task, _emit_event = False).execute(context = ctx.value)
            except CancelledError as e:
                if ctx.agent.cancel_event.is_set():
                    raise
                else:
                    return Result.Err(ErrorInfo(
                        error=e.reason,
                        details=e.reason
                        ))
    return agent_run

def agent_run_parallel_factory(agent_getter: AgentGetterProtocol, max_workers: int = 4):
    @except_safe
    def agent_run_parallel(ctx: ToolCallContext, tasks: list[str], names: Optional[list[str]] = None ) -> list[Result[str, ErrorInfo]]:
        """
        Same as `agent_run`, but designed to execute multiple tasks in parallel using separate sub-agents for each task. 
        This is useful when you have a batch of independent tasks that can be executed concurrently to save time.

        Note that same as `agent_run`, each sub-agent will have a blank context and cannot access the parent conversation history unless explicitly included in the instruction.
        You should provide all necessary context and clear, concise instructions for each task to ensure successful execution.

        Input: A list of clear, self-contained instructions, and an optional list of names for the sub-agents.
        (the number of names should match the number of tasks if provided; if not provided, sub-agents will be named automatically)

        Output: A list of final output messages from each sub-agent, in the same order as the input tasks. If any sub-agent encounters an issue during execution, its corresponding output will be an error message instead.
        """
        if names is not None and len(names) != len(tasks):
            return [Result.Err(ErrorInfo(
                    error=f"The number of names does not match the number of tasks",
                    details=f"Number of names: {len(names)}, Number of tasks: {len(tasks)}")
                )]
        names_list = names if names is not None else [f"agent-{i+1}" for i in range(len(tasks))]
        
        task_list = tasks
        results: list[Result | None] = [None] * len(task_list)
        agent_run = agent_run_factory(agent_getter)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index: dict[concurrent.futures.Future, int] = {}
            try:
                future_to_index = {
                    executor.submit(agent_run, ctx, task, name): idx
                    for idx, (task, name) in enumerate(zip(task_list, names_list))
                }
                for future in concurrent.futures.as_completed(future_to_index):
                    idx = future_to_index[future]
                    result = future.result()
                    results[idx] = result
            except BaseException as exc:
                for future in future_to_index:
                    future.cancel()
                # Only Ctrl+C must abort sibling workers: they never receive SIGINT.
                if isinstance(exc, KeyboardInterrupt):
                    ctx.agent.cancel()
                raise

        return cast(list[Result[str, ErrorInfo]], results)
    return agent_run_parallel