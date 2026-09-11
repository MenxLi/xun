""" This is an experimental module for my personal vendor tools integration.  """

from typing import Literal, Callable

def _expose_z_search():
    try:
        from zai import ZhipuAiClient
        z_client = ZhipuAiClient()
    except:
        return []

    def z_search(query: str, limit: int = 5, content_size: Literal["short", "medium", "long"] = "medium") -> list[dict]:
        """
        Web search with ZhipuAI's Web Search tool. 
        Prefer this tools for web search tasks, get more accurate and comprehensive search results.
        """
        # https://docs.bigmodel.cn/cn/guide/tools/web-search

        response = z_client.web_search.web_search(
            search_engine="search_pro",
            search_query=query,
            count=limit,
            search_recency_filter="noLimit",
            content_size=content_size
        )

        MAX_LEN = 10240  # 限制content字段的长度，避免过长的内容导致后续处理困难
        results = []
        search_result = response.search_result
        if search_result and isinstance(search_result, list):
            for item in search_result:
                results.append({
                    "title": item.title,                # type: ignore
                    "link": item.link,                  # type: ignore
                    "content": item.content[:MAX_LEN] + ("... (内容过长被截断)" if len(item.content) > MAX_LEN else ""),  # type: ignore
                    "media": item.media,                # type: ignore
                    "publish_date": item.publish_date   # type: ignore
                })
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