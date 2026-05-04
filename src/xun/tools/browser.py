from collections.abc import Callable
from typing import Literal, TypeVar
import functools, time

import html_to_markdown
from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import sync_playwright
from .fs import path_check


WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]
PageResult = TypeVar("PageResult")


def _slice_content(content: str, start_char: int, max_chars: int) -> str:
    page_content = content[start_char:start_char + max_chars]

    if start_char > 0 or start_char + max_chars < len(content):
        page_content += "\n\n[Content truncated due to length limits...]"

    return page_content

def _get_ttl_hash():
    # https://stackoverflow.com/a/55900800
    return round(time.time() / 3600)

class Browser:
    def __init__(self):
        # test if playwright can be launched successfully
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            browser.close()
        
    def _with_page(self, timeout_ms: int, action: Callable[[Page], PageResult]) -> PageResult:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()

            try:
                return self._run_with_context(context, timeout_ms, action)
            finally:
                context.close()
                browser.close()

    def _run_with_context(
        self,
        context: BrowserContext,
        timeout_ms: int,
        action: Callable[[Page], PageResult],
    ) -> PageResult:
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        try:
            return action(page)
        finally:
            page.close()

    def take_screenshot(
        self,
        url: str,
        full_page=False,
        wait_until: WaitUntil = "domcontentloaded",
        timeout_ms: int = 15000,
    ) -> bytes:
        def _capture(page):
            page.goto(url, wait_until=wait_until)
            return page.screenshot(full_page=full_page)

        return self._with_page(timeout_ms, _capture)

    @functools.lru_cache(maxsize=32)
    def get_page_html(
        self,
        url: str,
        wait_until: WaitUntil = "domcontentloaded",
        timeout_ms: int = 15000,
        ttl_hash: str | int | None = None,
    ) -> str:
        del ttl_hash
        def _load(page):
            page.goto(url, wait_until=wait_until)
            return page.content()

        return self._with_page(timeout_ms, _load)

    def browser_get_page(
        self,
        url: str,
        start_char: int = 0,
        max_chars: int = 100000,
        wait_until: WaitUntil = "domcontentloaded",
        timeout_ms: int = 15000,
    ) -> str:
        """
        Get the rendered HTML content of a web page and return it as markdown.
        """
        if start_char < 0:
            raise ValueError("start_char must be greater than or equal to 0.")

        if max_chars < 1:
            raise ValueError("max_chars must be greater than 0.")

        html = self.get_page_html(url, wait_until=wait_until, timeout_ms=timeout_ms, ttl_hash=_get_ttl_hash())
        r = html_to_markdown.convert(html)
        if not r.content:
            raise RuntimeError("Failed to convert HTML to markdown.")

        return _slice_content(r.content, start_char, max_chars)
    
    def browser_take_screenshot(
        self,
        url: str,
        save_to: str,
        full_page=False,
        wait_until: WaitUntil = "domcontentloaded",
        timeout_ms: int = 15000,
    ) -> Literal['screenshot_saved']:
        path_check(save_to)
        blob = self.take_screenshot(url, full_page=full_page, wait_until=wait_until, timeout_ms=timeout_ms)
        with open(save_to, "wb") as f:
            f.write(blob)
        return "screenshot_saved"

def expose_browser_tools() -> list[Callable]:
    import rich
    try:
        browser = Browser()
        return [browser.browser_get_page, browser.browser_take_screenshot]
    except Exception as e:
        rich.print(f"[Warning] Failed to initialize Browser tools: {e}. Skip registering browser tools.")
        return []