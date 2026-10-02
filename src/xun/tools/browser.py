from __future__ import annotations

import atexit
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from queue import Queue
from typing import Literal, cast
from typing_extensions import NotRequired, TypedDict

from ..types import TypeVar

import html_to_markdown
from PIL import Image as PILImage
from playwright.sync_api import (
    Browser as PlaywrightBrowser,
    BrowserContext,
    ConsoleMessage,
    FloatRect,
    Page,
    Playwright,
    Request,
    Response,
    ViewportSize,
    sync_playwright,
)

from ..hooks import HookArgs
from ..toolcall import ToolCallContext as Context, tool_attr
from ..types import JsonType
from .common import defer_tool_image, resolve_path


WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]
PageAction = Literal["list", "new", "select", "close"]
PageCommand = Literal["list", "new", "navigate", "reload", "select", "close"]
InteractAction = Literal["click", "fill", "press", "select", "wait"]
SnapshotFormat = Literal["accessibility", "html", "markdown"]
LogKind = Literal["all", "console", "pageerror", "request", "response", "requestfailed"]
ElementState = Literal["attached", "detached", "visible", "hidden"]
BrowserResult = TypeVar("BrowserResult")
MAX_LOG_ENTRIES = 500
_RUNTIME_HOOKS_STATE_KEY = "_browser_runtime_hooks"
DEFAULT_VIEWPORT: ViewportSize = {"width": 1280, "height": 720}

# page.evaluate has no native timeout: a never-settling expression would wedge the
# worker thread forever. These wrappers race the expression against a timer in JS,
# so the call always returns. They mirror native semantics: the expression is
# evaluated, and a resulting function is called with (argument), or (element,
# argument) for the element variant. A synchronous infinite loop cannot be
# interrupted by JS; only settled-later async work is bounded.
_EVALUATE_TIMEOUT_JS = """
async ([expression, argument, timeout]) => {
  let timer;
  const expiry = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`browser_evaluate timed out after ${timeout}ms`)), timeout);
  });
  try {
    return await Promise.race([
      (async () => {
        const value = eval(expression);
        return await (typeof value === 'function' ? value(argument) : value);
      })(),
      expiry,
    ]);
  } finally {
    clearTimeout(timer);
  }
}
"""

_ELEMENT_EVALUATE_TIMEOUT_JS = """
async (element, [expression, argument, timeout]) => {
  let timer;
  const expiry = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`browser_evaluate timed out after ${timeout}ms`)), timeout);
  });
  try {
    return await Promise.race([
      (async () => {
        const value = eval(expression);
        return await (typeof value === 'function' ? value(element, argument) : value);
      })(),
      expiry,
    ]);
  } finally {
    clearTimeout(timer);
  }
}
"""


class BrowserViewport(TypedDict):
    width: int
    height: int


class ScreenshotClip(TypedDict):
    x: float
    y: float
    width: float
    height: float


class PageInfo(TypedDict):
    page_id: str
    url: str
    title: str
    active: bool
    viewport: BrowserViewport | None


class SnapshotResult(TypedDict):
    page_id: str
    url: str
    title: str
    format: SnapshotFormat
    content: str
    start_char: int
    total_chars: int
    truncated: bool
    next_start_char: int | None


class BrowserLogEntry(TypedDict):
    kind: Literal["console", "pageerror", "request", "response", "requestfailed"]
    text: NotRequired[str]
    level: NotRequired[str]
    method: NotRequired[str]
    url: NotRequired[str]
    status: NotRequired[int]
    resource_type: NotRequired[str]
    location: NotRequired[dict[str, JsonType]]


class BrowserLogsResult(TypedDict):
    page_id: str
    entries: list[BrowserLogEntry]


class ScreenshotResult(TypedDict):
    page_id: str
    width: int
    height: int
    path: str | None


@dataclass(frozen=True)
class ScreenshotCapture:
    page_id: str
    data: bytes
    path: str | None


@dataclass
class BrowserPage:
    page_id: str
    page: Page
    logs: deque[BrowserLogEntry] = field(default_factory=lambda: deque(maxlen=MAX_LOG_ENTRIES))


@dataclass
class BrowserSession:
    context: BrowserContext
    pages: dict[str, BrowserPage] = field(default_factory=dict)
    active_page_id: str | None = None
    next_page_number: int = 1


@dataclass
class _Task:
    action: Callable[[], object]
    future: Future[object]


class BrowserRuntime:
    """Own Playwright on one worker thread and expose synchronous operations."""

    def __init__(self) -> None:
        self._tasks: Queue[_Task | None] = Queue()
        self._start_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False

        # These fields are only accessed by the worker thread.
        self._playwright: Playwright | None = None
        self._browser: PlaywrightBrowser | None = None
        self._sessions: dict[str, BrowserSession] = {}
        atexit.register(self.shutdown)

    def _ensure_worker(self) -> None:
        with self._start_lock:
            if self._closed:
                raise RuntimeError("Browser runtime is closed.")
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._worker,
                name="xun-browser",
                daemon=True,
            )
            self._thread.start()

    def _worker(self) -> None:
        while (task := self._tasks.get()) is not None:
            if not task.future.set_running_or_notify_cancel():
                continue
            try:
                task.future.set_result(task.action())
            except BaseException as exc:
                task.future.set_exception(exc)

    def _submit(self, action: Callable[[], BrowserResult]) -> BrowserResult:
        self._ensure_worker()
        future: Future[object] = Future()
        self._tasks.put(_Task(cast(Callable[[], object], action), future))
        return cast(BrowserResult, future.result())

    def _ensure_browser(self) -> PlaywrightBrowser:
        if self._browser is None:
            self._playwright = sync_playwright().start()
            try:
                self._browser = self._playwright.chromium.launch()
            except BaseException:
                self._playwright.stop()
                self._playwright = None
                raise
        return self._browser

    def _ensure_session(self, agent_id: str) -> BrowserSession:
        if session := self._sessions.get(agent_id):
            return session

        context = self._ensure_browser().new_context(viewport=DEFAULT_VIEWPORT)
        session = BrowserSession(context=context)
        self._sessions[agent_id] = session

        def track_page(page: Page) -> None:
            self._track_page(session, page)

        context.on("page", track_page)
        self._track_page(session, context.new_page())
        return session

    def _track_page(self, session: BrowserSession, page: Page) -> BrowserPage:
        for browser_page in session.pages.values():
            if browser_page.page is page:
                session.active_page_id = browser_page.page_id
                return browser_page

        page_id = f"page-{session.next_page_number}"
        session.next_page_number += 1
        browser_page = BrowserPage(page_id=page_id, page=page)
        session.pages[page_id] = browser_page
        session.active_page_id = page_id

        page.on("console", lambda message: self._log_console(browser_page, message))
        page.on("pageerror", lambda error: browser_page.logs.append({
            "kind": "pageerror",
            "text": str(error),
        }))
        page.on("request", lambda request: self._log_request(browser_page, request))
        page.on("response", lambda response: self._log_response(browser_page, response))
        page.on("requestfailed", lambda request: browser_page.logs.append({
            "kind": "requestfailed",
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "text": request.failure or "Request failed",
        }))
        def forget_page(_page: Page) -> None:
            self._forget_page(session, page_id)

        page.on("close", forget_page)
        return browser_page

    @staticmethod
    def _log_console(browser_page: BrowserPage, message: ConsoleMessage) -> None:
        browser_page.logs.append({
            "kind": "console",
            "level": message.type,
            "text": message.text,
            "location": cast(dict[str, JsonType], message.location),
        })

    @staticmethod
    def _log_request(browser_page: BrowserPage, request: Request) -> None:
        browser_page.logs.append({
            "kind": "request",
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
        })

    @staticmethod
    def _log_response(browser_page: BrowserPage, response: Response) -> None:
        browser_page.logs.append({
            "kind": "response",
            "status": response.status,
            "url": response.url,
        })

    @staticmethod
    def _forget_page(session: BrowserSession, page_id: str) -> None:
        session.pages.pop(page_id, None)
        if session.active_page_id == page_id:
            session.active_page_id = next(reversed(session.pages), None)

    def _get_page(self, agent_id: str, page_id: str | None) -> BrowserPage:
        session = self._ensure_session(agent_id)
        selected_page_id = page_id or session.active_page_id
        if selected_page_id is None:
            return self._track_page(session, session.context.new_page())
        try:
            return session.pages[selected_page_id]
        except KeyError as exc:
            raise ValueError(f"Unknown page_id: {selected_page_id}") from exc

    @staticmethod
    def _page_info(session: BrowserSession, browser_page: BrowserPage) -> PageInfo:
        return {
            "page_id": browser_page.page_id,
            "url": browser_page.page.url,
            "title": browser_page.page.title(),
            "active": browser_page.page_id == session.active_page_id,
            "viewport": cast(BrowserViewport | None, browser_page.page.viewport_size),
        }

    def navigate(
        self,
        agent_id: str,
        url: str,
        page_id: str | None,
        wait_until: WaitUntil,
        timeout_ms: int,
    ) -> PageInfo:
        def action() -> PageInfo:
            browser_page = self._get_page(agent_id, page_id)
            browser_page.page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            session = self._sessions[agent_id]
            session.active_page_id = browser_page.page_id
            return self._page_info(session, browser_page)

        return self._submit(action)

    def reload(
        self,
        agent_id: str,
        page_id: str | None,
        wait_until: WaitUntil,
        timeout_ms: int,
    ) -> PageInfo:
        def action() -> PageInfo:
            browser_page = self._get_page(agent_id, page_id)
            browser_page.page.reload(wait_until=wait_until, timeout=timeout_ms)
            return self._page_info(self._sessions[agent_id], browser_page)

        return self._submit(action)

    def pages(
        self,
        agent_id: str,
        action_name: PageAction,
        page_id: str | None,
        url: str | None,
    ) -> list[PageInfo] | PageInfo:
        def action() -> list[PageInfo] | PageInfo:
            session = self._ensure_session(agent_id)
            if action_name == "list":
                return [self._page_info(session, page) for page in session.pages.values()]
            if action_name == "new":
                browser_page = self._track_page(session, session.context.new_page())
                if url is not None:
                    browser_page.page.goto(url, wait_until="domcontentloaded")
                return self._page_info(session, browser_page)
            if page_id is None:
                raise ValueError(f"page_id is required for the '{action_name}' action.")
            browser_page = self._get_page(agent_id, page_id)
            if action_name == "select":
                session.active_page_id = page_id
                browser_page.page.bring_to_front()
                return self._page_info(session, browser_page)

            browser_page.page.close()
            if not session.pages:
                self._track_page(session, session.context.new_page())
            return [self._page_info(session, page) for page in session.pages.values()]

        return self._submit(action)

    def resize(self, agent_id: str, width: int, height: int, page_id: str | None) -> PageInfo:
        if width < 1 or height < 1:
            raise ValueError("width and height must be greater than 0.")

        def action() -> PageInfo:
            browser_page = self._get_page(agent_id, page_id)
            browser_page.page.set_viewport_size({"width": width, "height": height})
            return self._page_info(self._sessions[agent_id], browser_page)

        return self._submit(action)

    def snapshot(
        self,
        agent_id: str,
        page_id: str | None,
        format_name: SnapshotFormat,
        selector: str | None,
        start_char: int,
        max_chars: int,
    ) -> SnapshotResult:
        if start_char < 0:
            raise ValueError("start_char must be greater than or equal to 0.")
        if max_chars < 1:
            raise ValueError("max_chars must be greater than 0.")

        def action() -> SnapshotResult:
            browser_page = self._get_page(agent_id, page_id)
            page = browser_page.page
            locator = page.locator(selector or "body")
            if format_name == "accessibility":
                aria_snapshot = cast(Callable[..., str], locator.aria_snapshot)
                content = aria_snapshot(mode="ai")
            else:
                html = locator.inner_html() if selector else page.content()
                if format_name == "html":
                    content = html
                else:
                    converted = html_to_markdown.convert(html)
                    content = converted.content or ""

            total_chars = len(content)
            end_char = min(start_char + max_chars, total_chars)
            return {
                "page_id": browser_page.page_id,
                "url": page.url,
                "title": page.title(),
                "format": format_name,
                "content": content[start_char:end_char],
                "start_char": start_char,
                "total_chars": total_chars,
                "truncated": start_char > 0 or end_char < total_chars,
                "next_start_char": end_char if end_char < total_chars else None,
            }

        return self._submit(action)

    def click(self, agent_id: str, selector: str, page_id: str | None, timeout_ms: int) -> PageInfo:
        def action() -> PageInfo:
            browser_page = self._get_page(agent_id, page_id)
            browser_page.page.locator(selector).click(timeout=timeout_ms)
            return self._page_info(self._sessions[agent_id], browser_page)

        return self._submit(action)

    def fill(
        self,
        agent_id: str,
        selector: str,
        value: str,
        page_id: str | None,
        timeout_ms: int,
    ) -> PageInfo:
        def action() -> PageInfo:
            browser_page = self._get_page(agent_id, page_id)
            browser_page.page.locator(selector).fill(value, timeout=timeout_ms)
            return self._page_info(self._sessions[agent_id], browser_page)

        return self._submit(action)

    def press(
        self,
        agent_id: str,
        key: str,
        selector: str | None,
        page_id: str | None,
        timeout_ms: int,
    ) -> PageInfo:
        def action() -> PageInfo:
            browser_page = self._get_page(agent_id, page_id)
            if selector is None:
                browser_page.page.keyboard.press(key)
            else:
                browser_page.page.locator(selector).press(key, timeout=timeout_ms)
            return self._page_info(self._sessions[agent_id], browser_page)

        return self._submit(action)

    def select(
        self,
        agent_id: str,
        selector: str,
        values: list[str],
        page_id: str | None,
        timeout_ms: int,
    ) -> list[str]:
        return self._submit(lambda: self._get_page(agent_id, page_id).page.locator(selector).select_option(
            value=values,
            timeout=timeout_ms,
        ))

    def wait_for(
        self,
        agent_id: str,
        selector: str,
        state: ElementState,
        page_id: str | None,
        timeout_ms: int,
    ) -> PageInfo:
        def action() -> PageInfo:
            browser_page = self._get_page(agent_id, page_id)
            browser_page.page.locator(selector).wait_for(state=state, timeout=timeout_ms)
            return self._page_info(self._sessions[agent_id], browser_page)

        return self._submit(action)

    def evaluate(
        self,
        agent_id: str,
        expression: str,
        argument: JsonType,
        selector: str | None,
        page_id: str | None,
        timeout_ms: int,
    ) -> JsonType:
        if timeout_ms < 0:
            raise ValueError("timeout_ms must be greater than or equal to 0.")

        def action() -> JsonType:
            page = self._get_page(agent_id, page_id).page
            if timeout_ms == 0:
                result = (
                    page.locator(selector).evaluate(expression, argument)
                    if selector is not None
                    else page.evaluate(expression, argument)
                )
            elif selector is not None:
                result = page.locator(selector).evaluate(
                    _ELEMENT_EVALUATE_TIMEOUT_JS,
                    [expression, argument, timeout_ms],
                    timeout=timeout_ms,
                )
            else:
                result = page.evaluate(
                    _EVALUATE_TIMEOUT_JS,
                    [expression, argument, timeout_ms],
                )
            return cast(JsonType, result)

        return self._submit(action)

    def logs(
        self,
        agent_id: str,
        page_id: str | None,
        kind: LogKind,
        clear: bool,
    ) -> BrowserLogsResult:
        def action() -> BrowserLogsResult:
            browser_page = self._get_page(agent_id, page_id)
            entries = [entry for entry in browser_page.logs if kind == "all" or entry["kind"] == kind]
            if clear:
                if kind == "all":
                    browser_page.logs.clear()
                else:
                    retained = [entry for entry in browser_page.logs if entry["kind"] != kind]
                    browser_page.logs.clear()
                    browser_page.logs.extend(retained)
            return {"page_id": browser_page.page_id, "entries": entries}

        return self._submit(action)

    def screenshot(
        self,
        agent_id: str,
        path: Path | None,
        page_id: str | None,
        selector: str | None,
        clip: ScreenshotClip | None,
        full_page: bool,
        timeout_ms: int,
    ) -> ScreenshotCapture:
        selected_modes = int(selector is not None) + int(clip is not None) + int(full_page)
        if selected_modes > 1:
            raise ValueError("selector, clip, and full_page=True are mutually exclusive.")
        if clip is not None and (clip["width"] <= 0 or clip["height"] <= 0):
            raise ValueError("clip width and height must be greater than 0.")

        def action() -> ScreenshotCapture:
            browser_page = self._get_page(agent_id, page_id)
            if selector is not None:
                data = browser_page.page.locator(selector).screenshot(path=path, timeout=timeout_ms)
            else:
                data = browser_page.page.screenshot(
                    path=path,
                    clip=cast(FloatRect | None, clip),
                    full_page=full_page,
                    timeout=timeout_ms,
                )
            return ScreenshotCapture(
                page_id=browser_page.page_id,
                data=data,
                path=str(path.resolve()) if path is not None else None,
            )

        return self._submit(action)

    def close_session(self, agent_id: str) -> None:
        def action() -> None:
            session = self._sessions.pop(agent_id, None)
            if session is not None:
                session.context.close()
            if not self._sessions:
                self._close_browser()

        if self._thread is not None and not self._closed:
            self._submit(action)

    def _close_browser(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def shutdown(self) -> None:
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is None:
            return

        future: Future[object] = Future()

        def close_all() -> None:
            for session in self._sessions.values():
                session.context.close()
            self._sessions.clear()
            self._close_browser()

        self._tasks.put(_Task(close_all, future))
        future.result()
        self._tasks.put(None)
        thread.join()


def expose_browser_tools() -> list[Callable]:
    runtime = BrowserRuntime()

    def prepare(ctx: Context) -> str:
        agent_id = ctx.agent.identifier
        registered = ctx.agent.state.setdefault(_RUNTIME_HOOKS_STATE_KEY, set())
        runtime_id = id(runtime)
        if runtime_id not in registered:
            def cleanup(args: HookArgs.BeforeFinalizeArgs) -> None:
                runtime.close_session(args.agent.identifier)

            ctx.agent.hooks.before_finalize.add(cleanup)
            registered.add(runtime_id)
        return agent_id

    def browser_page(
        ctx: Context,
        action: PageCommand = "list",
        page_id: str | None = None,
        url: str | None = None,
        wait_until: WaitUntil = "domcontentloaded",
        timeout_ms: int = 15000,
    ) -> list[PageInfo] | PageInfo:
        """
        Manage persistent browser pages. Omit page_id to target the active page.

        Actions:
        - list: list all pages and identify the active one.
        - new: create and activate a page; url is optional.
        - navigate: open url in the selected page; url is required.
        - reload: reload the selected page.
        - select: make page_id the active page.
        - close: close page_id and select another remaining page.

        Pages retain cookies, storage, and state across browser tool calls.
        """
        agent_id = prepare(ctx)
        if action == "navigate":
            if url is None:
                raise ValueError("url is required for the 'navigate' action.")
            return runtime.navigate(agent_id, url, page_id, wait_until, timeout_ms)
        if action == "reload":
            return runtime.reload(agent_id, page_id, wait_until, timeout_ms)
        return runtime.pages(agent_id, cast(PageAction, action), page_id, url)

    def browser_resize(
        ctx: Context,
        width: int,
        height: int,
        page_id: str | None = None,
    ) -> PageInfo:
        """
        Set the viewport size of the active or selected page in CSS pixels.

        Use this before screenshots or responsive-layout checks that require exact dimensions.
        Both width and height must be positive integers. The new size persists for the page.
        """
        return runtime.resize(prepare(ctx), width, height, page_id)

    def browser_snapshot(
        ctx: Context,
        page_id: str | None = None,
        format: SnapshotFormat = "accessibility",
        selector: str | None = None,
        start_char: int = 0,
        max_chars: int = 50000,
    ) -> SnapshotResult:
        """
        Read the active or selected page without taking a screenshot.

        Prefer the default accessibility format for inspecting UI controls and page structure.
        Use markdown for readable document content, or html when exact DOM markup is needed.
        selector optionally limits the result to one Playwright locator. Long results are paged
        with start_char and max_chars; use next_start_char from the result to continue reading.
        """
        return runtime.snapshot(prepare(ctx), page_id, format, selector, start_char, max_chars)

    def browser_interact(
        ctx: Context,
        action: InteractAction,
        selector: str | None = None,
        value: str | list[str] | None = None,
        state: ElementState = "visible",
        page_id: str | None = None,
        timeout_ms: int = 15000,
    ) -> PageInfo | list[str]:
        """
        Interact with the active or selected page using Playwright selectors.

        Actions:
        - click: click selector.
        - fill: replace the value of selector; value must be a string.
        - press: press the key in value, such as Enter or Control+A; selector is optional.
        - select: select one or more option values; value may be a string or list of strings.
        - wait: wait for selector to reach state: attached, detached, visible, or hidden.

        Inspect the page with browser_snapshot first when the correct selector is unknown.
        """
        agent_id = prepare(ctx)
        if action == "press":
            if not isinstance(value, str):
                raise ValueError("a string value containing the key is required for 'press'.")
            return runtime.press(agent_id, value, selector, page_id, timeout_ms)
        if selector is None:
            raise ValueError(f"selector is required for the '{action}' action.")
        if action == "click":
            return runtime.click(agent_id, selector, page_id, timeout_ms)
        if action == "fill":
            if not isinstance(value, str):
                raise ValueError("a string value is required for 'fill'.")
            return runtime.fill(agent_id, selector, value, page_id, timeout_ms)
        if action == "select":
            values = [value] if isinstance(value, str) else value
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError("a string or list of strings is required for 'select'.")
            return runtime.select(agent_id, selector, values, page_id, timeout_ms)
        return runtime.wait_for(agent_id, selector, state, page_id, timeout_ms)

    def browser_evaluate(
        ctx: Context,
        expression: str,
        argument: JsonType = None,
        selector: str | None = None,
        page_id: str | None = None,
        timeout_ms: int = 15000,
    ) -> JsonType:
        """
        Evaluate JavaScript in the active or selected page and return JSON-compatible data.

        Without selector, expression runs with page.evaluate, for example `() => document.title`.
        With selector, it runs on the matched element, for example `element => element.textContent`.
        argument is passed as the function argument (or second argument for element evaluation).
        The call fails if the expression has not settled within timeout_ms; the timed-out
        async work keeps running in the page, and a synchronous infinite loop cannot be
        interrupted. Set timeout_ms=0 to remove the limit.
        Use this for developer inspection or operations not covered by browser_interact.
        """
        return runtime.evaluate(prepare(ctx), expression, argument, selector, page_id, timeout_ms)

    def browser_logs(
        ctx: Context,
        page_id: str | None = None,
        kind: LogKind = "all",
        clear: bool = False,
    ) -> BrowserLogsResult:
        """
        Read events captured for the active or selected page since it was opened.

        kind may be all, console, pageerror, request, response, or requestfailed.
        Set clear=True to remove the returned kind after reading it. Logs are bounded, so read
        them soon after reproducing an issue when debugging console or network behavior.
        """
        return runtime.logs(prepare(ctx), page_id, kind, clear)

    @tool_attr(required_capabilities=["vision"])
    def browser_screenshot(
        ctx: Context,
        page_id: str | None = None,
        selector: str | None = None,
        clip: ScreenshotClip | None = None,
        full_page: bool = False,
        save_to: str | None = None,
        timeout_ms: int = 15000,
    ) -> ScreenshotResult:
        """
        Capture the active or selected page and add the image to the next model context.

        By default, capture only the current viewport. Set selector to capture one element,
        clip to capture a page rectangle with x, y, width, and height, or full_page=True to
        capture the entire scrollable page. These three modes are mutually exclusive.
        Use browser_resize first when a specific viewport is required.
        save_to optionally also writes the PNG inside the agent work or temporary directory.
        """
        path = resolve_path(ctx, save_to).path if save_to is not None else None
        capture = runtime.screenshot(
            prepare(ctx),
            path,
            page_id,
            selector,
            clip,
            full_page,
            timeout_ms,
        )
        with PILImage.open(BytesIO(capture.data)) as image:
            width, height = image.size
            defer_tool_image(ctx, image.copy(), msg=f"[Tool browser_screenshot] Image size: {image.size}")
        return {
            "page_id": capture.page_id,
            "width": width,
            "height": height,
            "path": capture.path,
        }

    return [
        browser_page,
        browser_resize,
        browser_snapshot,
        browser_interact,
        browser_evaluate,
        browser_logs,
        browser_screenshot,
    ]
