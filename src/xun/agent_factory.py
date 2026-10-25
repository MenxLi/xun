from dataclasses import dataclass, field
from typing import Optional, Callable, TYPE_CHECKING, Literal, TypedDict
from threading import Event, Lock, Thread
from .types import CancelledError
from .toolcall import ToolCallContext
from .error_catch import ErrorInfo, except_safe, Result
from .agent_state import RuntimeEntry
import uuid, time
if TYPE_CHECKING:
    from .agent import Agent

SUBAGENT_POOL_FLAG = '__subagents'

class AgentWaitTimeout(TypedDict):
    status: Literal["timeout"]
    id: str
    notice: str
    background_jobs: list[dict]

class AgentWaitFinished(TypedDict):
    status: Literal["finished"]
    result: str
    background_jobs: list[dict]

@dataclass
class AgentThread:
    finished: Event
    agent: Optional["Agent[Agent.T.Init]"] = None
    result: Optional[Result[str, ErrorInfo]] = None

    def info(self) -> dict:
        return {
            "name": self.agent.name if self.agent is not None else None,
            "status": "finished" if self.finished.is_set() else "running"
        }

@dataclass
class AgentPool(RuntimeEntry[dict[str, AgentThread]]):
    lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    @classmethod
    def of(cls, agent: "Agent[Agent.T.Init]") -> "AgentPool":
        """The agent's pool, created on first use with a finalize-time shutdown hook."""
        def create() -> AgentPool:
            pool = cls({})
            agent.hooks.before_finalize.add(lambda _: pool.shutdown())
            return pool
        return agent.state.get_entry(SUBAGENT_POOL_FLAG, or_else = create)

    def add(self, identifier: str, agent_t: AgentThread) -> None:
        with self.lock:
            self.value[identifier] = agent_t

    def get(self, identifier: str) -> Optional[AgentThread]:
        with self.lock:
            return self.value.get(identifier)

    def remove(self, identifier: str) -> None:
        with self.lock:
            self.value.pop(identifier, None)

    def shutdown(self) -> None:
        """Cancel all tracked sub-agents and empty the pool."""
        with self.lock:
            threads = list(self.value.values())
            self.value.clear()
        for t in threads:
            if t.agent is not None:
                t.agent.cancel()

@dataclass
class AgentGetterParam:
    tool_context: ToolCallContext
    name: Optional[str] = None

AgentGetterProtocol = Callable[["AgentGetterParam"], "Agent[Agent.T.Uninit]"]

def agent_run_factory(agent_getter: AgentGetterProtocol):

    def agent_run_impl(ctx: ToolCallContext, task: str, name: Optional[str], agent_t: AgentThread) -> None:
        """Worker body: never raises, so background threads always signal completion."""
        try:
            param = AgentGetterParam(tool_context = ctx, name = name)
            with agent_getter(param) as agent:
                agent_t.agent = agent
                agent_t.result = agent.instruct(task, _emit_event = False).execute(context = ctx.value)
        except CancelledError as e:
            agent_t.result = Result.Err(ErrorInfo(error=e.reason, details=e.reason))
        except BaseException as e:
            agent_t.result = Result.Err(ErrorInfo(error=str(e), details=repr(e)))
        finally:
            agent_t.finished.set()
    
    def agent_thread_run(ctx: ToolCallContext, task: str, name: Optional[str] = None) -> str:
        identifier = uuid.uuid4().hex[:8]
        agent_t = AgentThread(finished=Event())
        AgentPool.of(ctx.agent).add(identifier, agent_t)
        Thread(
            target=agent_run_impl, args=(ctx, task, name, agent_t),
            name=f"subagent-{identifier}", daemon=True,
        ).start()
        return identifier
    
    def agent_wait_impl(ctx: ToolCallContext, identifier: str, timeout_seconds: Optional[int]) -> Result[str, ErrorInfo] | AgentWaitTimeout:
        pool = AgentPool.of(ctx.agent)
        agent_t = pool.get(identifier)
        if agent_t is None:
            raise ValueError(f"Agent not found: {identifier}")
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        try:
            while not agent_t.finished.wait(timeout=0.5):
                ctx.agent.check_cancel()
                if deadline is not None and time.monotonic() > deadline:
                    return {
                        "status": "timeout",
                        "id": identifier,
                        "notice": "the agent is still running; call agent_wait again with this id to keep waiting",
                        "background_jobs": agent_list_impl(ctx),
                    }
        except (CancelledError, KeyboardInterrupt) as e:
            # hand chained children their own flag (the chained one dies at the
            # parent's next run entry); detached children (cancel chain cut by a
            # custom getter) survive our cancellation
            if (child := agent_t.agent) is not None \
                and child.cancel_event.is_chain_of(ctx.agent.cancel_event):
                child.cancel()
            raise e
        pool.remove(identifier)
        ctx.agent.check_cancel()
        assert agent_t.result is not None
        return agent_t.result
    
    def agent_list_impl(ctx: ToolCallContext) -> list[dict]:
        with (pool := AgentPool.of(ctx.agent)).lock:
            return [
                {"id": identifier, **thr.info()}
                for identifier, thr in pool.value.items()
            ]
    
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
        identifier = agent_thread_run(ctx, task, name)
        r = agent_wait_impl(ctx, identifier, timeout_seconds=None)
        assert isinstance(r, Result)  # unreachable without a deadline
        return r
    
    @except_safe
    def agent_spawn(ctx: ToolCallContext, task: str, name: Optional[str] = None) -> dict[Literal['id'], str]:
        """
        Spawns an agent to run in the background without blocking the main thread.

        Same as `agent_run`, but executes the agent in the background and immediately returns its identifier.
        Call `agent_wait` with this identifier to retrieve the result. 
        Call `agent_list` to see all currently running background agents.
        """
        identifier = agent_thread_run(ctx, task, name)
        return {"id": identifier}
    
    @except_safe
    def agent_wait(ctx: ToolCallContext, identifier: str, timeout_seconds: Optional[int] = 300) -> Result[AgentWaitFinished | AgentWaitTimeout, ErrorInfo]:
        """
        Waits for a spawned agent to complete its task. 
        Will remove the agent from the pool once it has finished. 

        Input: The identifier of the background agent and an optional timeout in seconds
            (pass null to wait indefinitely).
        Output: A discriminated object: {"status": "finished", "result": ...} with the agent's final output
            message when it finishes, or {"status": "timeout", "id": ..., "notice": ...} if the timeout
            is reached before the agent finishes. Both also carry "background_jobs": every tracked
            background agent as {"id", "name", "status"}.

        Notes:
        • On timeout the agent keeps running (it stays in background_jobs); wait again with the same
          identifier to keep collecting its result.
        • A job listed as "finished" in background_jobs has a pending result; wait on its id to collect it.
        """
        r = agent_wait_impl(ctx, identifier, timeout_seconds)
        if isinstance(r, dict):       # timeout
            return Result.Ok(r)
        assert isinstance(r, Result)  # r should be a Result at this point
        if r.is_ok():
            return Result.Ok({"status": "finished", "result": r.unwrap(), "background_jobs": agent_list_impl(ctx)})
        else:
            return Result.Err(r.unwrap_err())
    
    @except_safe
    def agent_list(ctx: ToolCallContext) -> list[dict]:
        """
        Lists all background agents and their status, including finished ones whose
        results have not been retrieved yet. Use this if you forget the identifiers.

        Input: The tool call context.
        Output: A list of {"id": ..., "name": ..., "status": "running" | "finished"} for each tracked agent.
        """
        return agent_list_impl(ctx)

    return [agent_run, agent_spawn, agent_wait, agent_list]
