"""Tests for the stat extension: pricing, schema, budgets, hooks and the CLI."""
from __future__ import annotations

import os
import sqlite3
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall, Function)
from pydantic import ValidationError

from xun import (Agent, CancelledError, ErrorInfo, ExtensionContext, NullDisplay, Result,
                ToolBox, ToolResultType)
from xun.display_event import ErrorEvent, HTMLInfoEvent
from xun.extension import Extension
from xun.hooks import HookArgs
from xun.workspace import Workspace

from . import budget, cli
from .db import (DATA_VERSION, TOKEN, TOOLCALL, BudgetRow, ToolCallRow, TokenRow, default_db_path,
                 init_db, writer)
from .pricing import PRICING, format_count, parse_count
from .query import Filters, query_stats
from .setup_extension import setup_extension

WED_NOON = datetime(2026, 11, 11, 12, 0)  # Wednesday: this week started Monday the 9th
TODAY, IN_WEEK, IN_MONTH, OUT_OF_MONTH = (
    datetime(2026, 11, 11, 11), datetime(2026, 11, 10, 9),
    datetime(2026, 11, 3, 12), datetime(2026, 10, 22, 12),
)


class _DbTestBase(unittest.TestCase):
    """A temp XUN_HOME and stat.db path, with `_XUN_USERNAME` set."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "stat.db"
        self.env = patch.dict(os.environ, {"XUN_HOME": str(self.root / "home"), "_XUN_USERNAME": "alice"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def log_token(self, when: datetime, *, prompt: int = 0, cached: int = 0, completion: int = 0,
                  user: str = "alice") -> float:
        row = TokenRow(model_call_id=f"call-{when:%Y%m%d%H}-{user}", user=user, timestamp=when.timestamp(),
                       prompt_tokens=prompt, total_tokens=prompt + completion,
                       completion_tokens=completion, prompt_tokens_cached=cached)
        with writer(self.db_path) as conn:
            TOKEN.insert(conn, [row])
        return PRICING.cost(prompt_tokens=prompt, cached_prompt_tokens=cached, completion_tokens=completion)

    def log_tool(self, name: str, *, toolcall_id: str, success: bool | None = None,
                 when: datetime | None = None, user: str = "alice") -> None:
        row = ToolCallRow(toolcall_id=toolcall_id, toolname=name, user=user,
                          timestamp=(when or TODAY).timestamp(), success=success)
        with writer(self.db_path) as conn:
            TOOLCALL.insert(conn, [row])

    def call_cli(self, *argv: str) -> int:
        with redirect_stdout(StringIO()):
            return cli.run(cli.build_parser().parse_args([*argv, "--db", str(self.db_path)]))


class PricingTest(unittest.TestCase):
    def test_cost_weights_each_token_class(self) -> None:
        cost = PRICING.cost(prompt_tokens=1000, cached_prompt_tokens=400, completion_tokens=100)
        self.assertEqual(cost, PRICING.cached_prompt * 400 + PRICING.prompt * 600 + PRICING.completion * 100)
        self.assertEqual((PRICING.cached_prompt, PRICING.prompt, PRICING.completion), (0.05, 0.2, 1.0))

    def test_uncached_never_goes_negative(self) -> None:
        self.assertEqual(PRICING.cost(prompt_tokens=100, cached_prompt_tokens=200, completion_tokens=0), PRICING.cached_prompt * 200)

    def test_counts_get_a_unit(self) -> None:
        self.assertEqual([format_count(v) for v in (None, 0, 999, 1000, 1_234_567, 2.5e9)],
                         ["-", "0", "999", "1.00K", "1.23M", "2.50B"])

    def test_counts_parse_back(self) -> None:
        self.assertEqual([parse_count(v) for v in ("999", "50K", "1.5M", "2b")], [999, 50_000, 1_500_000, 2e9])
        self.assertAlmostEqual(parse_count(format_count(12_345)), 12_345, delta=12_345 * 0.01)


class SchemaTest(_DbTestBase):
    def test_init_creates_every_table(self) -> None:
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"toolcall", "token", "token_budget"} <= tables)

    def test_old_database_gains_the_cached_column(self) -> None:
        """A db written before prompt_tokens_cached was tracked keeps working and is repriced."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE token (id INTEGER PRIMARY KEY AUTOINCREMENT, model_call_id TEXT,
                            user TEXT, timestamp FLOAT, completion_tokens INTEGER, prompt_tokens INTEGER,
                            total_tokens INTEGER)""")
            conn.execute("INSERT INTO token (model_call_id, user, timestamp, completion_tokens,"
                         " prompt_tokens, total_tokens) VALUES ('m1', 'alice', ?, 100, 1000, 1100)",
                         (TODAY.timestamp(),))
        init_db(self.db_path)
        stats = query_stats(self.db_path, Filters())
        self.assertEqual(stats.tokens.cached_tokens, 0)
        self.assertEqual(stats.tokens.cost,
                         PRICING.cost(prompt_tokens=1000, cached_prompt_tokens=0, completion_tokens=100))

    def test_sql_constraints_reject_nonsense(self) -> None:
        init_db(self.db_path)
        with self.assertRaises(sqlite3.IntegrityError), writer(self.db_path) as conn:
            conn.execute("INSERT INTO token (model_call_id, user, timestamp, completion_tokens,"
                         " prompt_tokens, total_tokens) VALUES ('m', 'u', 1, -1, 1, 0)")
        with self.assertRaises(sqlite3.IntegrityError), writer(self.db_path) as conn:
            conn.execute("INSERT INTO token_budget (username, per_day) VALUES ('u', 0)")

    def test_row_models_validate_too(self) -> None:
        with self.assertRaises(ValidationError):
            BudgetRow(username="u", per_day=0)
        with self.assertRaises(ValidationError):
            TokenRow(model_call_id="m", timestamp=1, prompt_tokens=-5)


class BudgetTest(_DbTestBase):
    def setUp(self) -> None:
        super().setUp()
        init_db(self.db_path)
        self.c_today = self.log_token(TODAY, prompt=1000, cached=400, completion=100)
        self.c_week = self.log_token(IN_WEEK, prompt=100, completion=10)
        self.c_month = self.log_token(IN_MONTH, prompt=10, completion=1)
        self.log_token(OUT_OF_MONTH, prompt=10_000, completion=1000)

    def test_period_sums_follow_natural_boundaries(self) -> None:
        spent = {p.kind: p.cost for p in budget.usage(self.db_path, "alice", now=WED_NOON).periods}
        self.assertEqual(spent["day"], self.c_today)
        self.assertEqual(spent["week"], self.c_today + self.c_week)
        self.assertEqual(spent["month"], self.c_today + self.c_week + self.c_month)

    def test_other_users_are_not_counted(self) -> None:
        self.log_token(TODAY, prompt=999, user="bob")
        spent = budget.usage(self.db_path, "alice", now=WED_NOON).periods[0]
        self.assertEqual(spent.cost, self.c_today)

    def test_no_row_means_unlimited(self) -> None:
        usage = budget.usage(self.db_path, "alice", now=WED_NOON)
        self.assertEqual([p.budget for p in usage.periods], [None, None, None])
        budget.check_budget(self.db_path, "alice", now=WED_NOON)  # must not raise

    def test_set_limits_keeps_periods_it_was_not_given(self) -> None:
        budget.set_limits(self.db_path, "alice", {"day": 1000, "month": 1e6})
        budget.set_limits(self.db_path, "alice", {"week": 5000})
        row = budget.get(self.db_path, "alice")
        assert row is not None
        self.assertEqual((row.per_day, row.per_week, row.per_month), (1000, 5000, 1e6))
        self.assertTrue(budget.clear(self.db_path, "alice"))
        self.assertIsNone(budget.get(self.db_path, "alice"))
        self.assertFalse(budget.clear(self.db_path, "alice"))

    def test_a_ceiling_that_is_reached_cancels_with_a_reason(self) -> None:
        budget.set_limits(self.db_path, "alice", {"week": self.c_today + self.c_week})
        with self.assertRaises(CancelledError) as caught:
            budget.check_budget(self.db_path, "alice", now=WED_NOON)
        self.assertIn("week", str(caught.exception.reason))
        self.assertNotIn("this month", str(caught.exception.reason))

    def test_a_ceiling_that_is_not_reached_passes(self) -> None:
        budget.set_limits(self.db_path, "alice", {"week": self.c_today + self.c_week + 1.0})
        budget.check_budget(self.db_path, "alice", now=WED_NOON)  # must not raise


class StatsQueryTest(_DbTestBase):
    def test_totals_and_daily_rows_are_sums(self) -> None:
        init_db(self.db_path)
        self.log_token(TODAY, prompt=1000, cached=400, completion=100)
        self.log_token(IN_MONTH, prompt=500, completion=50, user="bob")
        self.log_tool("bash", toolcall_id="t1", success=True)
        self.log_tool("bash", toolcall_id="t2", success=False)
        self.log_tool("read_file", toolcall_id="t3")
        stats = query_stats(self.db_path, Filters())
        self.assertEqual(stats.tools.calls, 3)
        self.assertEqual((stats.tools.ok, stats.tools.fail, stats.tools.pending), (1, 1, 1))
        self.assertEqual([tool.toolname for tool in stats.by_tool], ["bash", "read_file"])
        self.assertEqual(stats.tokens.prompt_tokens, 1500)  # summed, not averaged
        self.assertEqual(stats.tokens.cached_tokens, 400)
        self.assertEqual(stats.tokens.total_tokens, 1650)
        today = stats.daily[0]
        self.assertEqual((today.day, today.calls, today.prompt_tokens, today.total_tokens),
                         ("2026-11-11", 3, 1000, 1100))

    def test_a_typoed_user_shows_who_exists(self) -> None:
        init_db(self.db_path)
        self.log_tool("bash", toolcall_id="t1", success=True)
        stats = query_stats(self.db_path, Filters(user="nobody"))
        self.assertEqual(stats.tools.calls, 0)
        self.assertEqual(stats.known_users, ["alice"])


class _AgentTestBase(_DbTestBase):
    """An initialized agent whose only extension is this one."""

    def setUp(self) -> None:
        super().setUp()
        agent = Agent(display=NullDisplay(), toolbox=ToolBox().with_defaults(),
                      workspace=Workspace(workdir=self.root))
        agent.config.model.name = "test-model"
        self.agent = agent.initialize()
        ext = Extension(name="stat", description="", setup=setup_extension, path=Path("extensions/stat"),
                        data_version=DATA_VERSION)
        # the context is typed against an agent still initializing
        setup_extension(ExtensionContext(_ext=ext, agent=self.agent))  # type: ignore[arg-type]
        self.db_path = default_db_path()

    def record_model_call(self, *, prompt: int, cached: int, completion: int) -> None:
        self.agent.hooks.completion_token_update.invoke(HookArgs.TokenMetrics(
            model_call_id="m1", prompt_tokens=prompt, prompt_tokens_cached=cached,
            completion_tokens=completion, total_tokens=prompt + completion))

    def record_tool_call(self, toolname: str, *, ok: bool = True) -> None:
        call = ChatCompletionMessageFunctionToolCall(
            id="call-1", type="function", function=Function(name=toolname, arguments="{}"))
        self.agent.hooks.before_tool_call.invoke(
            HookArgs.BeforeToolCallArgs(agent=self.agent, tool_calls=[call]))
        result: ToolResultType = (Result.Ok("done") if ok
                                  else Result.Err(ErrorInfo(error="boom", details="boom")))
        self.agent.hooks.after_tool_call.invoke(
            HookArgs.AfterToolCallArgs(agent=self.agent, tool_results=[("call-1", result)]))

    def run_stat(self, *args: str) -> list[object]:
        command = self.agent.command.get("stat")
        assert command is not None
        before = len(self.agent.display.events())
        command.invoke(self.agent, list(args))
        return [event.payload for event in self.agent.display.events()[before:]]

    def execute_now(self) -> None:
        self.agent.hooks.before_execution.invoke(HookArgs.BeforeExecutionArgs(
            agent=self.agent, schema=None, max_iterations=1))


class ExtensionTest(_AgentTestBase):
    def test_data_lands_in_the_extension_data_dir(self) -> None:
        self.assertTrue(self.db_path.exists())

    def test_tool_calls_are_logged_with_their_result(self) -> None:
        self.record_tool_call("bash")
        stats = query_stats(self.db_path, Filters())
        self.assertEqual((stats.tools.calls, stats.tools.ok), (1, 1))

    def test_unregistered_tools_are_ignored(self) -> None:
        self.record_tool_call("not_a_tool")
        self.assertEqual(query_stats(self.db_path, Filters()).tools.calls, 0)

    def test_cached_tokens_are_recorded(self) -> None:
        self.record_model_call(prompt=1000, cached=400, completion=100)
        tokens = query_stats(self.db_path, Filters()).tokens
        self.assertEqual((tokens.cached_tokens, tokens.prompt_tokens), (400, 1000))
        self.assertEqual(tokens.cost, PRICING.cost(prompt_tokens=1000, cached_prompt_tokens=400,
                                                   completion_tokens=100))

    def test_stat_command_reports_cached_tokens_and_budget(self) -> None:
        self.record_model_call(prompt=1000, cached=400, completion=100)
        events = [p for p in self.run_stat() if isinstance(p, HTMLInfoEvent)]
        self.assertEqual(len(events), 1)
        text = events[0].to_text()
        self.assertIn("cached 400", text)
        self.assertIn("Budget (alice)", text)
        self.assertIn("unlimited", text)

    def test_stat_command_rejects_a_bad_option(self) -> None:
        events = self.run_stat("days=seven")
        self.assertTrue(any(isinstance(p, ErrorEvent) for p in events))

    def test_execution_is_refused_once_the_budget_is_spent(self) -> None:
        self.record_model_call(prompt=1000, cached=400, completion=100)
        budget.set_limits(self.db_path, "alice", {"day": 10})
        with self.assertRaises(CancelledError) as caught:
            self.execute_now()
        self.assertIn("Token budget exceeded for user 'alice'", str(caught.exception.reason))

    def test_execution_proceeds_under_the_budget(self) -> None:
        self.record_model_call(prompt=10, cached=0, completion=1)
        budget.set_limits(self.db_path, "alice", {"day": 1e6})
        self.execute_now()  # must not raise


class CliTest(_DbTestBase):
    def test_show_reports_a_missing_database(self) -> None:
        self.assertEqual(self.call_cli("show"), 1)

    def test_budget_clear_creates_no_database(self) -> None:
        self.assertEqual(self.call_cli("budget", "clear", "--user", "alice"), 1)
        self.assertFalse(self.db_path.exists())

    def test_budget_set_accepts_units(self) -> None:
        self.assertEqual(self.call_cli("budget", "set", "--user", "alice", "--day", "50K"), 0)
        row = budget.get(self.db_path, "alice")
        assert row is not None
        self.assertEqual((row.per_day, row.per_week, row.per_month), (50_000, None, None))
        self.assertEqual(self.call_cli("budget", "show"), 0)

    def test_budget_none_clears_a_ceiling(self) -> None:
        self.call_cli("budget", "set", "--user", "alice", "--day", "50K")
        self.call_cli("budget", "set", "--user", "alice", "--day", "none")
        row = budget.get(self.db_path, "alice")
        assert row is not None
        self.assertIsNone(row.per_day)

    def test_budget_set_needs_a_period(self) -> None:
        self.assertEqual(self.call_cli("budget", "set", "--user", "alice"), 2)

    def test_show_prints_a_report(self) -> None:
        init_db(self.db_path)
        self.log_token(TODAY, prompt=1500, cached=600, completion=150)
        with patch.dict(os.environ, {"_XUN_USERNAME": "alice"}), redirect_stdout(StringIO()) as out:
            rc = cli.run(cli.build_parser().parse_args(["show", "--db", str(self.db_path)]))
        self.assertEqual(rc, 0)
        self.assertIn("1.50K", out.getvalue())  # prompt tokens, in units
        self.assertIn("unlimited", out.getvalue())


if __name__ == "__main__":
    unittest.main()
