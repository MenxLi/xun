"""Web search tool backed by an agent-driven browser sub-agent.

The tool spawns a focused sub-agent that drives the existing browser tools
(`expose_browser_tools`) to query a search engine reachable from the host's
region and reports structured results.

Two hard-won design points:

- The sub-agent runs WITHOUT `schema=`: passing a schema makes the execution
  loop send `response_format: json_schema` and inject "respond in JSON now",
  which some OpenAI-compatible backends honor by answering immediately and
  skipping tool calls entirely — the agent then "searches" from model memory
  in ~1s without ever opening the browser. A plain `execute()` keeps the
  normal tool-calling loop; the final answer is requested as JSON in text.

- Fabrication guard: tool-call hooks record whether a real `browser_snapshot`
  ever returned page content. Results with no browse evidence are still
  returned (information beats errors), but flagged with a WARNING in notes.
"""

from __future__ import annotations

import json, hashlib
from typing import TYPE_CHECKING, Callable, Optional
from urllib.parse import urlparse

import json_repair
from pydantic import BaseModel, Field, ValidationError

from ..hooks import HookArgs
from ..toolcall import ToolCallContext

if TYPE_CHECKING:
    from ..agent import Agent

SEARCH_MAX_ITERATIONS = 24
SEARCH_MAX_RESULTS_LIMIT = 20

# browser tools whose successful output means the agent really read a page
_READ_TOOLS = frozenset({"browser_snapshot", "browser_evaluate"})
_MIN_EVIDENCE_CHARS = 80  # real pages snapshot much larger; error pages are tiny

# One shared BrowserRuntime (worker thread + chromium) for all searches:
# sessions are keyed per agent and closed when each search agent finalizes.
_shared_browser_tools: Optional[list[Callable]] = None


def _shared_browser_tools_once() -> list[Callable]:
    global _shared_browser_tools
    if _shared_browser_tools is None:
        from .browser import expose_browser_tools
        _shared_browser_tools = expose_browser_tools()
    return _shared_browser_tools


class SearchResultItem(BaseModel):
    """One search result extracted from the results page."""

    title: str = Field(description="Result title as shown on the results page.")
    url: str = Field(description="Destination URL of the result, copied from the page.")
    snippet: str = Field(default="", description="Snippet shown under the result, empty if none.")


class WebSearchOutput(BaseModel):
    """Structured output of a web search."""

    query: str = Field(default="", description="The search query actually used.")
    engine: str = Field(default="", description="Search engine the results came from, e.g. 'cn.bing', 'baidu'.")
    results: list[SearchResultItem] = Field(
        default_factory=list,
        description="Organic results in page order. Empty only if nothing usable was found; explain in notes.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Optional: engines tried, blocks hit, or why results are empty.",
    )


SEARCH_SYSTEM_PROMPT = """\
You are a web search agent. You MUST obtain answers by actually browsing; you may use memory for nothing else.

Mandatory order of operations (your FIRST message must be a tool call, never an answer):
1. system_info + datetime: infer the host's country from timezone, utc_offset, locale.
2. browser_page(action="navigate", url=...): open a search engine reachable there. In mainland China Google/DuckDuckGo are GFW-blocked, so use https://cn.bing.com/search?q=<query> or https://www.baidu.com/s?wd=<query>; elsewhere https://www.bing.com/search?q=<query>, https://duckduckgo.com/html/?q=<query> or Google. url-encode the query. If an engine fails or blocks you, try another.
3. browser_snapshot(format="markdown"): read the results page (start_char to page if truncated).
4. Only after the snapshot shows real results: reply with ONLY a JSON object (no other text, no code fences):
{"query": "...", "engine": "...", "results": [{"title": "...", "url": "...", "snippet": "..."}], "notes": "..."}
Copy titles, URLs and snippets verbatim from the snapshot; skip ads. Never invent or guess URLs. If you truly find nothing, return "results": [] and explain in "notes".
"""

_JSON_RECOVERY = (
    "Your last message was not a valid JSON object. Reply again with ONLY the final "
    "JSON object described in the system prompt, nothing else."
)


class _BrowseEvidence:
    """Records via tool-call hooks whether a real browser read succeeded."""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self.max_read_chars = 0

    def on_before_tool_call(self, args: "HookArgs.BeforeToolCallArgs") -> None:
        for call in args.tool_calls:
            self._names[call.id] = call.function.name

    def on_after_tool_call(self, args: "HookArgs.AfterToolCallArgs") -> None:
        for tool_id, result in args.tool_results:
            if self._names.get(tool_id) not in _READ_TOOLS or not result.is_ok():
                continue
            value = result.value
            content = value.get("content") if isinstance(value, dict) else None
            if isinstance(content, str):
                size = len(content)
            else:
                size = len(result.value_str())
            self.max_read_chars = max(self.max_read_chars, size)

    @property
    def browsed(self) -> bool:
        return self.max_read_chars >= _MIN_EVIDENCE_CHARS


def _extract_json(text: str) -> Optional[WebSearchOutput]:
    """Parse the sub-agent's final message into WebSearchOutput, tolerating prose."""
    candidates = [text.strip()]
    # first balanced top-level {...} block, in case the JSON is wrapped in prose
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for candidate in candidates:
        try:
            parsed = json_repair.loads(candidate)
            if isinstance(parsed, dict):
                return WebSearchOutput.model_validate(parsed)
        except (ValueError, ValidationError):
            continue
    return None


def _is_junk(item: SearchResultItem) -> bool:
    """Drop obviously useless items; err on the side of keeping them."""
    host = urlparse(item.url).netloc
    return not item.title.strip() or not host or "." not in host or "example.com" in host


def web_search(ctx: ToolCallContext, query: str, max_results: int = 5) -> WebSearchOutput:
    """
    Search the web with a browser-driven agent and return structured results.

    The sub-agent infers the host's region, picks a search engine reachable from
    there (GFW-aware), opens the results page with the browser tools, and copies
    results off the real page. Results it could not have browsed are still
    returned but carry a WARNING in notes (they may be model memory, verify
    before trusting). Empty results come with notes explaining why.

    Use this for current events, documentation, or any fact outside your
    knowledge. Keep the query keyword-focused. max_results is clamped to 1-20.

    Input: query (search keywords) and max_results (upper bound on results).
    Output: query, engine used, ordered results (title/url/snippet), notes.
    """
    from ..agent import Agent  # avoid circular import
    from ..toolbox import ToolBox
    from .system import system_info, system_time

    query = query.strip()
    if not query:
        raise ValueError("query must not be empty.")
    if max_results < 1:
        raise ValueError("max_results must be greater than 0.")
    max_results = min(max_results, SEARCH_MAX_RESULTS_LIMIT)

    # A dedicated agent: inherits parent config/cancel event, but gets a
    # minimal toolbox (browser + system info) so it cannot recurse or touch files.
    # share_display=False: the searcher runs silently on the default NullDisplay.
    agent = Agent.inherit(
        ctx.agent, 
        copy_toolbox=False, 
        copy_command=False, 
        share_display=False, 
        )
    agent.name = f"{ctx.agent.name}-search-{hashlib.md5(query.encode()).hexdigest()}"
    agent.toolbox = ToolBox().register(*_shared_browser_tools_once(), system_info, system_time)
    agent.system(SEARCH_SYSTEM_PROMPT)

    evidence = _BrowseEvidence()
    agent.hooks.before_tool_call.add(evidence.on_before_tool_call)
    agent.hooks.after_tool_call.add(evidence.on_after_tool_call)

    task = (
        f"Search the web for: {query}\n"
        f"Return up to {max_results} relevant results as the final JSON object."
    )

    # NOTE: deliberately execute() without schema= — schema mode forces JSON
    # response_format and suppresses the sub-agent's tool calls (see module doc).
    # execute() is except_safe-wrapped, so it returns Result[str, ErrorInfo].
    last_error = ""
    with agent as initialized:
        worker = initialized.instruct(task, _emit_event=False)
        res = worker.execute(max_iterations=SEARCH_MAX_ITERATIONS, context=ctx.value)
        text = res.unwrap() if res.is_ok() else ""
        if res.is_err():
            last_error = res.unwrap_err().error
        output = _extract_json(text) if text else None
        if output is None:
            # one retry in the same conversation: browsing state is kept
            res = worker.instruct(_JSON_RECOVERY, _emit_event=False).execute(
                max_iterations=SEARCH_MAX_ITERATIONS, context=ctx.value
            )
            text = res.unwrap() if res.is_ok() else ""
            if res.is_err():
                last_error = res.unwrap_err().error
            output = _extract_json(text) if text else None

    if output is None:
        detail = last_error or (repr(text[:300] + "...") if len(text) > 300 else repr(text))
        raise RuntimeError(f"Web search sub-agent produced no parseable result: {detail}")

    output.query = output.query.strip() or query
    if output.results:
        seen: set[str] = set()
        clean: list[SearchResultItem] = []
        for item in output.results:
            if not _is_junk(item) and item.url not in seen:
                seen.add(item.url)
                clean.append(item)
        output.results = clean[:max_results]
        if not evidence.browsed:
            warning = "WARNING: no successful page read was observed; results may be from model memory, verify before trusting."
            output.notes = f"{warning} {output.notes}" if output.notes else warning
    return output


def expose_search_tools() -> list[Callable]:
    return [web_search]


if __name__ == "__main__":
    from ..agent import Agent
    from ..displays.display import Display
    from ..toolcall import ToolCallContext

    query = input("Enter your search query: ")
    host = Agent(display=Display()).initialize()
    ctx = ToolCallContext(agent=host, tool_name="web_search", v=None)
    try:
        output = web_search(ctx, query)
    except Exception as e:
        print(f"Search failed: {e}")
    else:
        print(json.dumps(output.model_dump(), indent=2, ensure_ascii=False))
    host.finalize()
