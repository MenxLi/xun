import unittest
from types import SimpleNamespace

from xun.toolcall import ToolCallContext
from xun.tools.cmd import _confirmation_policy, _parse_command_spec


class CmdConfirmationPolicyTest(unittest.TestCase):
    def assertConfirmationRequired(self, command: str, expected: bool) -> None:
        ctx = ToolCallContext(SimpleNamespace(state={}), "bash", None)
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


if __name__ == "__main__":
    unittest.main()