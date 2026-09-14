""" This is an experimental module for my personal vendor tools integration.  """

from typing import Literal, Callable
import rich
from ..toolcall import tool_attr
from ..config import get_internal_env_bool

def _expose_z_search():
    try:
        from zai import ZhipuAiClient
        z_client = ZhipuAiClient()
    except Exception as e:
        if get_internal_env_bool("WARN_EXTRA_TOOL_REGISTRATION"):
            rich.print(f"[Warning] Failed to import ZhipuAiClient. Extra tool 'z_search' will not be available: {e}")
        return []

    @tool_attr(name="web_search", override=True)
    def z_search(
        query: str, 
        limit: int = 5, 
        max_content_length: int = 4096,
        ) -> list[dict]:
        """
        Web search with ZhipuAI's Web Search tool. 
        Prefer this tools for web search tasks, get more accurate and comprehensive search results.

        The maximum content length (per result, in characters) can be controlled via the `max_content_length` parameter.
        For details on any result, visit the link provided in the "link" field.
        """
        # THE API IS REALLY BAD AT DOCUMENTING... ALSO UNPREDICTABLE BEHAVIOR
        # https://docs.bigmodel.cn/cn/guide/tools/web-search

        response = z_client.web_search.web_search(
            search_engine="search_pro",
            search_query=query,
            count=limit,
            search_recency_filter="noLimit",

            # content size can be long... but that will be too much for context
            content_size='medium'
        )

        MAX_LEN = max_content_length  # 限制content字段的长度，避免过长的内容导致后续处理困难
        results = []
        search_result = response.search_result
        if search_result and isinstance(search_result, list):
            for i, item in enumerate(search_result):
                results.append({
                    "title": item.title,                # type: ignore
                    "link": item.link,                  # type: ignore
                    "content": item.content[:MAX_LEN] + ("... [truncated]" if len(item.content) > MAX_LEN else ""),  # type: ignore
                    "media": item.media,                # type: ignore
                    "publish_date": item.publish_date   # type: ignore
                })
                if i > limit - 1:
                    # zai will somehow over-return more results than requested...
                    break
        else:
            raise AssertionError("Unexpected response format from ZhipuAI Web Search tool: 'search_result' is missing or not a list.")
        return results
    return [z_search]

def expose_extra_tools() -> list[Callable]:
    """
    z_search: need to install zai-sdk and setup `ZAI_API_KEY` environment variable, 
        or will not be available silently if not configured.
    """
    return _expose_z_search()