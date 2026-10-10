import os
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from xun import Agent, NullDisplay, ToolBox
from xun.toolcall import ToolCallContext
from xun.agent_state import AgentState
from xun.tools.cmd import (
    CmdExecResult, CommandPool, bash, bash_background, bash_list, bash_read, bash_stop, bash_wait,
    expose_cmd_tools, run_command, truncate_output,
)
from xun.tools.cmd_policy import RiskAssessResult
from xun.tools.cmd_policy import _confirmation_policy, _parse_command_spec
from xun.types import CancelledError
from xun.workspace import DeferredTempDirectory, Workspace


class CmdConfirmationPolicyTest(unittest.TestCase):
    def assertConfirmationRequired(self, command: str, expected: bool) -> None:
        ctx = ToolCallContext(SimpleNamespace(state=AgentState()), "bash", None)
        policy = _confirmation_policy(ctx, _parse_command_spec(command))
        self.assertIs(policy.requires_confirmation, expected, command)

    def test_allowlisted_command_chain_is_auto_approved(self) -> None:
        for command in (
            "ls && pwd",
            "(ls && pwd) || echo ok",
            "ls ; pwd",
            "ls | wc",
            "ls | wc && echo ok",
            "echo $(pwd)",
            "echo $(ls | wc)",
            'echo "$(pwd)"',
            "echo '$(rm)'",
        ):
            with self.subTest(command=command):
                self.assertConfirmationRequired(command, False)

    def test_exact_safe_redirections_are_auto_approved(self) -> None:
        for command in (
            "ls 2>&1",
            "ls >/dev/null",
            "ls 1>/dev/null",
            "ls 2>/dev/null",
            "ls >/dev/null 2>&1",
        ):
            with self.subTest(command=command):
                self.assertConfirmationRequired(command, False)

    def test_chain_with_non_allowlisted_command_requires_confirmation(self) -> None:
        for command in (
            "ls && rm",
            "ls | rm",
            "ls ; rm",
            "echo $(rm)",
            'echo "$(rm)"',
            "echo $(echo $(rm))",
            "echo hi 2>&11",
            "echo hi 2>/dev/nullx",
        ):
            with self.subTest(command=command):
                self.assertConfirmationRequired(command, True)

    def test_unsupported_shell_syntax_requires_confirmation(self) -> None:
        for command in (
            "ls > out.txt",
            "ls 2>&2",
            "ls 1>&2",
            "ls >>/dev/null",
            "ls </dev/null",
            "ls &",
            "ls\npwd",
            "echo `pwd`",
        ):
            with self.subTest(command=command):
                self.assertConfirmationRequired(command, True)

    def test_allowlisted_prefix_with_extra_args_is_auto_approved(self) -> None:
        for command in (
            "git status --short --branch",
            "git --no-pager diff --cached",
            "git log -5",
            "git show --stat",
            "python -m pytest -q",
            "python -m unittest -v",
        ):
            with self.subTest(command=command):
                self.assertConfirmationRequired(command, False)

    def test_prefix_match_is_token_bound(self) -> None:
        for command in (
            "git statusx",
            "git difftool",
            "git push",
            "git checkout .",
            "python -m unittestx",
        ):
            with self.subTest(command=command):
                self.assertConfirmationRequired(command, True)

    def test_path_based_commands_require_confirmation(self) -> None:
        for command in (
            "/bin/ls",
            "./script.sh",
            "echo $(/bin/ls)",
        ):
            with self.subTest(command=command):
                self.assertConfirmationRequired(command, True)

    def test_parse_collects_all_command_heads(self) -> None:
        spec = _parse_command_spec("(ls | wc) && echo ok")
        self.assertEqual([command.value for command in spec.commands], ["ls", "wc", "echo"])


@unittest.skipIf(os.name == "nt", "bash tools are not exposed on Windows")
class CmdExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cwd = Path(directory.name)
        agent = Agent(display=NullDisplay(), workspace=Workspace(
            workdir=self.cwd, tempdir=DeferredTempDirectory(self.cwd),
        ))
        agent.config.enable_extensions = False
        agent.config.model.name = "test"
        self.agent = agent.initialize()
        self.addCleanup(self.agent.finalize)
        self.ctx = ToolCallContext(self.agent, "bash", None)
        self.assessment = self.enterContext(patch(
            "xun.tools.cmd_policy.agent_risk_assess",
            return_value=RiskAssessResult(policy="allow"),
        ))

    def command(self, code: str) -> str:
        return shlex.join([sys.executable, "-u", "-c", code])

    def start(self, code: str) -> str:
        return bash_background(self.ctx, self.command(code))["id"]

    def wait(
        self, identifier: str, timeout: float | None = 5, max_output_size: int | None = 16_000,
    ) -> CmdExecResult:
        result = bash_wait(self.ctx, identifier, timeout=timeout, max_output_size=max_output_size)
        assert "returncode" in result, result
        return result

    def await_output(self, identifier: str, expected: str) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if expected in bash_read(self.ctx, identifier)["stdout"]:
                return
            time.sleep(0.01)
        self.fail(f"Command did not produce {expected!r}")

    def test_blocking_command_preserves_result_environment_and_cwd(self) -> None:
        (self.cwd / "child").mkdir()
        original_cwd = Path.cwd()
        result = bash(
            self.ctx,
            self.command("import os, sys; print(os.getcwd()); print(os.getenv('XUN_TEST')); "
                         "print('error', file=sys.stderr); sys.exit(7)"),
            cd="child", envs={"XUN_TEST": "value"},
        )
        self.assertEqual(result["stdout"], f"{self.cwd / 'child'}\nvalue")
        self.assertEqual(result["stderr"], "error")
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(set(result), {"stdout", "stderr", "returncode", "duration"})
        self.assertEqual(Path.cwd(), original_cwd)
        self.assertNotIn("XUN_TEST", os.environ)

    def test_run_command_executes_literal_argv_without_shell(self) -> None:
        result = run_command(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", "literal; $HOME"],
            5, self.cwd, None, lambda: False,
        )
        self.assertEqual(result.stdout, "literal; $HOME\n")
        self.assertEqual(result.returncode, 0)

    def test_background_start_returns_before_exit_and_wait_timeout_keeps_running(self) -> None:
        identifier = self.start("import time; print('ready'); time.sleep(60)")
        self.await_output(identifier, "ready")
        job = CommandPool.of(self.ctx).get(identifier)
        self.assertIsNone(job.process.poll())
        for timeout in (0, 0.02):
            result = bash_wait(self.ctx, identifier, timeout=timeout)
            assert "status" in result
            self.assertEqual(result["status"], "timeout")
            self.assertEqual(result["id"], identifier)
            self.assertEqual(set(result), {"id", "status", "notice"})
            self.assertIsNone(job.process.poll())
        self.assertEqual(bash_list(self.ctx)[0]["status"], "running")
        self.assertIsNone(bash_read(self.ctx, identifier)["returncode"])

    def test_read_is_cumulative_and_does_not_move_child_write_offset(self) -> None:
        gate = self.cwd / "gate"
        identifier = self.start(
            "import pathlib, time; print('first'); "
            f"gate = pathlib.Path({str(gate)!r}); "
            "\nwhile not gate.exists(): time.sleep(0.01)\nprint('last')"
        )
        self.await_output(identifier, "first")
        for _ in range(3):
            self.assertEqual(bash_read(self.ctx, identifier)["stdout"], "first")
        gate.touch()
        result = self.wait(identifier, timeout=None)
        self.assertEqual(result["stdout"], "first\nlast")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(bash_list(self.ctx), [])

    def test_background_environment_cwd_and_nonzero_exit(self) -> None:
        (self.cwd / "child").mkdir()
        identifier = bash_background(
            self.ctx,
            self.command("import os, sys; print(os.getcwd()); print(os.getenv('XUN_TEST')); "
                         "print('error', file=sys.stderr); sys.exit(9)"),
            cd="child", envs={"XUN_TEST": "value"},
        )["id"]
        result = self.wait(identifier)
        self.assertEqual(result["stdout"], f"{self.cwd / 'child'}\nvalue")
        self.assertEqual(result["stderr"], "error")
        self.assertEqual(result["returncode"], 9)

    def test_finished_jobs_remain_until_collected_and_release_files(self) -> None:
        identifier = self.start("print('done')")
        pool = CommandPool.of(self.ctx)
        job = pool.get(identifier)
        self.assertTrue(job.finished.wait(5))
        self.assertEqual(bash_list(self.ctx)[0]["status"], "finished")
        snapshot = bash_read(self.ctx, identifier)
        self.assertEqual(snapshot["status"], "finished")
        self.assertEqual(snapshot["returncode"], 0)
        self.assertEqual(snapshot["stdout"], "done")
        time.sleep(0.02)
        self.assertEqual(bash_read(self.ctx, identifier)["duration"], snapshot["duration"])
        self.assertEqual(set(snapshot), {
            "id", "command", "status", "stdout", "stderr", "returncode", "duration",
        })
        with patch("xun.tools.cmd._signal_process") as signal_process:
            bash_stop(self.ctx, identifier)
            signal_process.assert_not_called()
        result = self.wait(identifier, timeout=0)
        self.assertEqual(result["stdout"], "done")
        self.assertTrue(job.stdout.closed)
        self.assertTrue(job.stderr.closed)
        with self.assertRaisesRegex(ValueError, "Command not found"):
            bash_read(self.ctx, identifier)

    def test_large_output_spools_both_streams_and_bounded_reads_preserve_head_tail(self) -> None:
        identifier = self.start(
            "import sys; print('HEAD' + 'x' * 1_000_000 + 'TAIL'); "
            "print('ERRHEAD' + 'y' * 1_000_000 + 'ERRTAIL', file=sys.stderr)"
        )
        self.assertTrue(CommandPool.of(self.ctx).get(identifier).finished.wait(5))
        with patch("xun.tools.cmd.os.pread", wraps=os.pread) as pread:
            snapshot = bash_read(self.ctx, identifier, max_output_size=32)
        self.assertTrue(all(call.args[1] <= 128 for call in pread.call_args_list))
        marker = "\n[... truncated output ...]\n"
        self.assertEqual(snapshot["stdout"], "HEAD" + "x" * 12 + marker + "x" * 12 + "TAIL")
        self.assertEqual(snapshot["stderr"], "ERRHEAD" + "y" * 9 + marker + "y" * 9 + "ERRTAIL")
        result = self.wait(identifier, max_output_size=None)
        self.assertEqual(len(result["stdout"]), 1_000_008)
        self.assertEqual(len(result["stderr"]), 1_000_014)

    def test_output_truncation_handles_small_limits_and_unicode(self) -> None:
        self.assertEqual(truncate_output("abcdef", 1), "a\n[... truncated output ...]\n")
        identifier = self.start("print('\\u4e2d' * 100 + '\\U0001f642' * 100)")
        result = self.wait(identifier, max_output_size=5)
        self.assertEqual(result["stdout"], "\u4e2d" * 3 + "\n[... truncated output ...]\n" + "\U0001f642" * 2)

    def test_stop_reaps_process_and_preserves_output(self) -> None:
        identifier = self.start("import time; print('ready'); time.sleep(60)")
        self.await_output(identifier, "ready")
        job = CommandPool.of(self.ctx).get(identifier)
        self.assertEqual(bash_stop(self.ctx, identifier)["status"], "finished")
        self.assertIsNotNone(job.process.poll())
        self.assertEqual(bash_read(self.ctx, identifier)["stdout"], "ready")
        self.assertEqual(bash_stop(self.ctx, identifier)["status"], "finished")
        result = self.wait(identifier)
        self.assertNotEqual(result["returncode"], 0)

    def test_stop_escalates_when_sigterm_is_ignored(self) -> None:
        identifier = self.start(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready'); time.sleep(60)"
        )
        self.await_output(identifier, "ready")
        job = CommandPool.of(self.ctx).get(identifier)
        bash_stop(self.ctx, identifier)
        self.assertTrue(job.finished.is_set())
        self.assertIsNotNone(job.process.poll())
        bash_wait(self.ctx, identifier)

    @unittest.skipUnless(sys.platform == "linux", "uses /proc to distinguish zombies")
    def test_stop_kills_surviving_descendants(self) -> None:
        ready = self.cwd / "child-ready"
        child_code = (
            "import signal, pathlib, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(ready)!r}).touch(); time.sleep(60)"
        )
        identifier = self.start(
            "import subprocess, sys, pathlib, time; "
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            f"\nwhile not pathlib.Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
            "print(child.pid); time.sleep(60)"
        )
        deadline = time.monotonic() + 5
        while not bash_read(self.ctx, identifier)["stdout"] and time.monotonic() < deadline:
            time.sleep(0.01)
        pid = int(bash_read(self.ctx, identifier)["stdout"])
        bash_stop(self.ctx, identifier)
        proc_status = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 5
        while proc_status.exists() and time.monotonic() < deadline:
            if proc_status.read_text().split()[2] == "Z":
                break
            time.sleep(0.01)
        self.assertTrue(not proc_status.exists() or proc_status.read_text().split()[2] == "Z")

    def test_cancellation_while_waiting_stops_job_and_retains_result(self) -> None:
        identifier = self.start("import time; print('ready'); time.sleep(60)")
        self.await_output(identifier, "ready")
        self.agent.cancel_event.event.set()
        with self.assertRaises(CancelledError):
            bash_wait(self.ctx, identifier)
        self.agent.cancel_event.event.clear()
        self.assertEqual(bash_read(self.ctx, identifier)["status"], "finished")
        self.assertEqual(self.wait(identifier)["stdout"], "ready")

    def test_finalize_cleans_all_jobs_and_temporary_files(self) -> None:
        identifiers = [self.start("import time; time.sleep(60)") for _ in range(2)]
        pool = CommandPool.of(self.ctx)
        jobs = [pool.get(identifier) for identifier in identifiers]
        self.agent.finalize()
        self.assertEqual(pool.value, {})
        for job in jobs:
            self.assertIsNotNone(job.process.poll())
            self.assertTrue(job.finished.is_set())
            self.assertTrue(job.stdout.closed)
            self.assertTrue(job.stderr.closed)
        with self.assertRaisesRegex(RuntimeError, "pool has shut down"):
            bash_background(self.ctx, "echo no")

    def test_invalid_identifiers_report_errors(self) -> None:
        for tool in (bash_read, bash_wait, bash_stop):
            with self.subTest(tool=tool.__name__):
                with self.assertRaisesRegex(ValueError, "Command not found"):
                    tool(self.ctx, "unknown")

    def test_pools_are_agent_scoped_and_not_serialized(self) -> None:
        identifier = self.start("print('done')")
        other = Agent.inherit(self.agent).initialize()
        self.addCleanup(other.finalize)
        other_ctx = ToolCallContext(other, "bash", None)
        self.assertEqual(bash_list(other_ctx), [])
        with self.assertRaisesRegex(ValueError, "Command not found"):
            bash_read(other_ctx, identifier)
        self.assertNotIn("__bash_jobs", self.agent.state.to_json())
        self.assertEqual(self.wait(identifier)["stdout"], "done")

    def test_concurrent_starts_share_one_pool(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as executor:
            handles = list(executor.map(lambda _: bash_background(self.ctx, "echo done"), range(8)))
        identifiers = {handle["id"] for handle in handles}
        self.assertEqual({info["id"] for info in bash_list(self.ctx)}, identifiers)
        for identifier in identifiers:
            self.assertEqual(self.wait(identifier)["stdout"], "done")
        self.assertEqual(bash_list(self.ctx), [])

    def test_background_stdin_is_closed(self) -> None:
        identifier = self.start("import sys; print(repr(sys.stdin.read()))")
        self.assertEqual(self.wait(identifier)["stdout"], "''")

    def test_cancelled_start_does_not_spawn(self) -> None:
        self.agent.cancel_event.event.set()
        with patch("xun.tools.cmd.subprocess.Popen") as spawn:
            with self.assertRaises(CancelledError):
                bash_background(self.ctx, "echo no")
            spawn.assert_not_called()
        self.agent.cancel_event.event.clear()
        self.assertEqual(bash_list(self.ctx), [])

    def test_invalid_options_do_not_start_jobs(self) -> None:
        for timeout in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                bash(self.ctx, "echo no", timeout=timeout)
            with self.assertRaises(ValueError):
                bash_wait(self.ctx, "unknown", timeout=timeout)
        for limit in (0, -1):
            with self.assertRaises(ValueError):
                bash(self.ctx, "echo no", max_output_size=limit)
            with self.assertRaises(ValueError):
                bash_read(self.ctx, "unknown", max_output_size=limit)
            with self.assertRaises(ValueError):
                bash_wait(self.ctx, "unknown", max_output_size=limit)
        self.assertEqual(bash_list(self.ctx), [])

    def test_rejected_commands_and_invalid_cwd_never_spawn(self) -> None:
        self.assessment.return_value = RiskAssessResult(policy="reject", reason="test rejection")
        self.enterContext(patch.object(self.agent.display, "get_choice", return_value="No"))
        with patch("xun.tools.cmd.subprocess.Popen") as spawn:
            for tool in (bash, bash_background):
                with self.subTest(tool=tool.__name__):
                    with self.assertRaisesRegex(RuntimeError, "rejected by confirmation"):
                        tool(self.ctx, self.command("print('no')"))
                    with self.assertRaisesRegex(ValueError, "not within agent"):
                        tool(self.ctx, "echo no", cd=str(self.cwd.parent))
            spawn.assert_not_called()
        self.assertEqual(bash_list(self.ctx), [])

    def test_spawn_failure_leaves_no_jobs_or_open_output_files(self) -> None:
        from tempfile import TemporaryFile
        streams = []

        def create_stream():
            stream = TemporaryFile()
            streams.append(stream)
            return stream

        with patch("xun.tools.cmd.subprocess.Popen", side_effect=OSError("spawn failed")):
            with patch("xun.tools.cmd.TemporaryFile", side_effect=create_stream) as create:
                with self.assertRaisesRegex(OSError, "spawn failed"):
                    bash_background(self.ctx, "echo no")
        self.assertEqual(bash_list(self.ctx), [])
        self.assertEqual(create.call_count, 2)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_blocking_timeout_and_cancellation_terminate_command(self) -> None:
        command = self.command("import time; time.sleep(60)")
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                if cancel:
                    self.agent.cancel_event.event.set()
                with self.assertRaises(CancelledError if cancel else RuntimeError):
                    bash(self.ctx, command, timeout=0.02)
                self.agent.cancel_event.event.clear()

    def test_tools_register_valid_schemas(self) -> None:
        from xun.toolcall import Function
        tools = expose_cmd_tools()
        self.assertEqual([tool.__name__ for tool in tools], [
            "bash", "bash_background", "bash_wait", "bash_read", "bash_list", "bash_stop",
        ])
        toolbox = ToolBox().with_defaults("cmd")
        for tool in tools:
            self.assertIsNotNone(toolbox.pop(tool.__name__))
        for tool in tools:
            function = Function.from_function(tool)
            self.assertEqual(function.context_param, "ctx")
            assert function.args_model is not None
            self.assertNotIn("ctx", function.args_model.model_fields)


if __name__ == "__main__":
    unittest.main()
from concurrent.futures import ThreadPoolExecutor