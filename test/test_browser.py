import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from PIL import Image

from xun.conversation import Conversation
from xun.hooks import HookArgs, Hooks
from xun.agent_state import AgentState
from xun.toolcall import ToolCallContext
from xun.tools.browser import BrowserPage, BrowserRuntime, ScreenshotCapture, expose_browser_tools
from xun.types import Result


class BrowserRuntimeTest(unittest.TestCase):
    def test_tool_registration_does_not_start_playwright(self) -> None:
        with patch("xun.tools.browser.sync_playwright") as playwright_factory:
            tools = expose_browser_tools()

        self.assertEqual(
            [tool.__name__ for tool in tools],
            [
                "browser_page",
                "browser_resize",
                "browser_snapshot",
                "browser_interact",
                "browser_evaluate",
                "browser_logs",
                "browser_screenshot",
            ],
        )
        playwright_factory.assert_not_called()

    def test_agent_registers_one_session_cleanup_hook(self) -> None:
        agent = SimpleNamespace(identifier="agent-1", state=AgentState(), hooks=Hooks())
        context = ToolCallContext(agent, "browser_page", None)
        browser_page = expose_browser_tools()[0]

        with (
            patch.object(BrowserRuntime, "pages", return_value=[]),
            patch.object(BrowserRuntime, "close_session") as close_session,
        ):
            browser_page(context, "list")
            browser_page(context, "list")
            agent.hooks.before_finalize.invoke(HookArgs.BeforeFinalizeArgs(agent=agent))

        close_session.assert_called_once_with("agent-1")

    def test_screenshot_is_deferred_into_conversation_without_saving(self) -> None:
        output = BytesIO()
        Image.new("RGB", (20, 10), "blue").save(output, format="PNG")
        conversation = Conversation()
        agent = SimpleNamespace(
            identifier="agent-1",
            state=AgentState(),
            hooks=Hooks(),
            conversation=conversation,
        )
        context = ToolCallContext(agent, "browser_screenshot", None)
        browser_screenshot = expose_browser_tools()[-1]

        with patch.object(
            BrowserRuntime,
            "screenshot",
            return_value=ScreenshotCapture("page-1", output.getvalue(), None),
        ):
            result = browser_screenshot(context)

        self.assertEqual(result, {"page_id": "page-1", "width": 20, "height": 10, "path": None})
        self.assertEqual(conversation.messages, [])
        conversation.add_tool_result("call-1", Result.Ok(result))
        agent.hooks.after_execution_step.invoke(HookArgs.AfterExecutionStepArgs(agent=agent))
        self.assertEqual([message.role for message in conversation.messages], ["tool", "user"])

    def test_screenshot_modes_are_mutually_exclusive(self) -> None:
        runtime = BrowserRuntime()
        try:
            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                runtime.screenshot(
                    "agent-1",
                    None,
                    None,
                    "#target",
                    {"x": 0, "y": 0, "width": 10, "height": 10},
                    False,
                    100,
                )
        finally:
            runtime.shutdown()

    def test_resize_has_dedicated_tool(self) -> None:
        agent = SimpleNamespace(identifier="agent-1", state=AgentState(), hooks=Hooks())
        context = ToolCallContext(agent, "browser_resize", None)
        browser_resize = expose_browser_tools()[1]

        with patch.object(BrowserRuntime, "resize", return_value={}) as resize:
            browser_resize(context, 640, 480)

        resize.assert_called_once_with("agent-1", 640, 480, None)

    def test_evaluate_bounds_js_with_timeout(self) -> None:
        runtime = BrowserRuntime()
        try:
            page = MagicMock()
            page.evaluate.return_value = "ok"
            with patch.object(BrowserRuntime, "_get_page", return_value=BrowserPage("page-1", page)):
                result = runtime.evaluate("agent-1", "() => 1", None, None, None, 1234)

            self.assertEqual(result, "ok")
            expression, argument = page.evaluate.call_args.args
            self.assertIn("Promise.race", expression)
            self.assertEqual(argument, ["() => 1", None, 1234])

            element_page = MagicMock()
            element_page.locator.return_value.evaluate.return_value = "el"
            with patch.object(BrowserRuntime, "_get_page", return_value=BrowserPage("page-2", element_page)):
                runtime.evaluate("agent-1", "el => 1", "a", "#x", None, 200)

            element_page.locator.assert_called_once_with("#x")
            expression, argument = element_page.locator.return_value.evaluate.call_args.args
            self.assertIn("Promise.race", expression)
            self.assertEqual(argument, ["el => 1", "a", 200])
            self.assertEqual(element_page.locator.return_value.evaluate.call_args.kwargs["timeout"], 200)

            raw_page = MagicMock()
            with patch.object(BrowserRuntime, "_get_page", return_value=BrowserPage("page-3", raw_page)):
                runtime.evaluate("agent-1", "1 + 2", None, None, None, 0)
            self.assertEqual(raw_page.evaluate.call_args.args, ("1 + 2", None))

            with self.assertRaisesRegex(ValueError, "timeout_ms"):
                runtime.evaluate("agent-1", "() => 1", None, None, None, -1)
        finally:
            runtime.shutdown()

    def test_page_reload_targets_active_page(self) -> None:
        agent = SimpleNamespace(identifier="agent-1", state=AgentState(), hooks=Hooks())
        context = ToolCallContext(agent, "browser_page", None)
        browser_page = expose_browser_tools()[0]

        with patch.object(BrowserRuntime, "reload", return_value={}) as reload:
            browser_page(context, "reload", wait_until="load", timeout_ms=5000)

        reload.assert_called_once_with("agent-1", None, "load", 5000)

    def test_evaluate_tool_defaults_and_passes_timeout(self) -> None:
        agent = SimpleNamespace(identifier="agent-1", state=AgentState(), hooks=Hooks())
        context = ToolCallContext(agent, "browser_evaluate", None)
        browser_evaluate = expose_browser_tools()[4]

        with patch.object(BrowserRuntime, "evaluate", return_value=None) as evaluate:
            browser_evaluate(context, "() => 1")
            browser_evaluate(context, "() => 1", timeout_ms=999)

        self.assertEqual(
            evaluate.call_args_list,
            [
                call("agent-1", "() => 1", None, None, None, 30000),
                call("agent-1", "() => 1", None, None, None, 999),
            ],
        )

    def test_tasks_from_different_threads_run_on_one_worker(self) -> None:
        runtime = BrowserRuntime()
        try:
            with ThreadPoolExecutor(max_workers=3) as executor:
                worker_ids = list(executor.map(
                    lambda _: runtime._submit(threading.get_ident),
                    range(6),
                ))

            self.assertEqual(len(set(worker_ids)), 1)
            self.assertNotEqual(worker_ids[0], threading.get_ident())
        finally:
            runtime.shutdown()

    def test_agents_have_separate_contexts_and_share_one_browser(self) -> None:
        playwright = MagicMock()
        browser = MagicMock()
        contexts = [MagicMock(), MagicMock()]
        pages = [MagicMock(), MagicMock()]
        for page in pages:
            page.url = "about:blank"
            page.title.return_value = ""
        for context, page in zip(contexts, pages):
            context.new_page.return_value = page

        playwright.chromium.launch.return_value = browser
        browser.new_context.side_effect = contexts
        factory = MagicMock()
        factory.start.return_value = playwright

        with patch("xun.tools.browser.sync_playwright", return_value=factory):
            runtime = BrowserRuntime()
            try:
                runtime.pages("agent-1", "list", None, None)
                runtime.pages("agent-1", "list", None, None)
                runtime.pages("agent-2", "list", None, None)

                factory.start.assert_called_once_with()
                playwright.chromium.launch.assert_called_once_with()
                self.assertEqual(browser.new_context.call_count, 2)
                self.assertEqual(
                    browser.new_context.call_args_list[0].kwargs["viewport"],
                    {"width": 1280, "height": 720},
                )

                runtime.close_session("agent-1")
                contexts[0].close.assert_called_once_with()
                browser.close.assert_not_called()

                runtime.close_session("agent-2")
                contexts[1].close.assert_called_once_with()
                browser.close.assert_called_once_with()
                playwright.stop.assert_called_once_with()
            finally:
                runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
