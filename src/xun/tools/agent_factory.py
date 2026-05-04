from typing import Optional, Callable, TYPE_CHECKING, Literal
import concurrent.futures
import contextvars
import json_repair
if TYPE_CHECKING:
    from ..agent import Agent

def agent_run_factory(agent_getter: Callable[[], "Agent"]):
    def agent_run( task: str, name: Optional[str] = None ) -> str:
        """
        Creates an isolated sub-agent to execute complex, multi-step tasks. 
        The new agent holds appropriate tools and capabilities to complete the assigned task, but starts with a blank context.

        Use this when:
        • The task is self-contained but requires multiple steps or heavy reasoning.
        • You need to isolate execution to avoid bloating the main conversation's context window.
        • The task does not require frequent back-and-forth with the parent agent.

        Input: A clear, self-contained instruction or tool directive specifying exactly what the new agent should do.
        Output: Returns the new agent's final output message upon successful completion, or `None` if it exits prematurely or encounters an error.

        Notes:
        • The new agent starts with a blank context and cannot access the parent conversation history unless explicitly included in the instruction.
        • Prefer instructing the new agent to return results directly in its final message. File I/O can also be used for larger outputs or intermediate results when necessary, but should explicitly be mentioned in the instruction.
        """
        agent = agent_getter()
        if name is not None:
            agent.name = name
        agent.instruct(task)
        try:
            return agent.execute()
        except Exception as e:
            print(f"Error in sub-agent: {e}")
            return f"[Error in sub-agent: {e}]"
    return agent_run

def agent_run_parallel_factory(agent_getter: Callable[[], "Agent"], max_workers: int = 4):
    def agent_run_parallel( tasks: list[str] | str, names: Optional[list[str] | str] = None ) -> list[Optional[str]]:
        """
        Same as `agent_run`, but designed to execute multiple tasks in parallel using separate sub-agents for each task. 
        This is useful when you have a batch of independent tasks that can be executed concurrently to save time.

        Note that same as `agent_run`, each sub-agent will have a blank context and cannot access the parent conversation history unless explicitly included in the instruction.
        You should provide all necessary context and clear, concise instructions for each task to ensure successful execution.

        Input: A list of clear, self-contained instructions, and an optional list of names for the sub-agents.
        (Must input a list of strings, if the input is string instead of list, it will try to be decoded as JSON list, and if that fails it will return an error message)
        (the number of names should match the number of tasks if provided; if not provided, sub-agents will be named automatically)

        Output: A list of final output messages from each sub-agent, in the same order as the input tasks. If any sub-agent encounters an error, its corresponding output will contain an error message.
        """
        def parse_list_str(inp) -> tuple[Literal[True], list[str]] | tuple[Literal[False], str]:
            """return (success, result), if success is True, result is the parsed list; if success is False, result is the error message"""
            if isinstance(inp, list):
                if all(isinstance(item, str) for item in inp):
                    return True, inp
                return False, "Not all items in the list are strings"
            elif isinstance(inp, str):
                try:
                    loaded = json_repair.loads(inp)
                    if isinstance(loaded, list) and all(isinstance(item, str) for item in loaded):
                        return True, loaded
                    else:
                        return False, "Parsed JSON is not a list of strings"
                except Exception as e:
                    return False, f"Error parsing input string as JSON list: {e}"
            else:
                return False, f"Invalid input type: {type(inp)}. Expected list or JSON string."
        
        task_parse_success, tasks_parse_return = parse_list_str(tasks)
        if not task_parse_success:
            return [f"[Error in parsing tasks input: {tasks_parse_return}]"]
        
        if names is not None:
            names_parse_success, names_parse_return = parse_list_str(names)
            if not names_parse_success:
                return [f"[Error in parsing names input: {names_parse_return}]"]
            if len(names_parse_return) != len(tasks_parse_return):
                return [f"[Error: The number of names does not match the number of tasks]"]
            names_list = names_parse_return
        else:
            names_list = [f"agent-{i+1}" for i in range(len(tasks_parse_return))]
        
        task_list = tasks_parse_return
        results: list[Optional[str]] = [None] * len(task_list)
        agent_run = agent_run_factory(agent_getter)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index = {
                executor.submit(contextvars.copy_context().run, agent_run, task, name): idx
                for idx, (task, name) in enumerate(zip(task_list, names_list))
            }
            for future in concurrent.futures.as_completed(future_to_index):
                idx = future_to_index[future]
                try:
                    result = future.result()
                    results[idx] = result
                except Exception as e:
                    results[idx] = f"[Error in sub-agent: {e}]"

        return results
    return agent_run_parallel