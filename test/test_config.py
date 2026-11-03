import os
import unittest
import tempfile
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from xun.core.config import get_home_dir


class HomeDirTest(unittest.TestCase):
    def test_env_var_wins(self) -> None:
        with patch.dict(os.environ, {"XUN_HOME": "/opt/xun-home"}):
            self.assertEqual(get_home_dir(), Path("/opt/xun-home"))

    def test_env_var_is_expanded_and_absolute(self) -> None:
        with patch.dict(os.environ, {"XUN_HOME": ".xun"}):
            self.assertEqual(get_home_dir(), Path.cwd() / ".xun")
        with patch.dict(os.environ, {"XUN_HOME": "~/.xun", "HOME": "/home/alice"}):
            self.assertEqual(get_home_dir(), Path("/home/alice/.xun"))

    def test_default_is_the_user_home(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_home_dir(), Path.home() / ".xun")


class EnvFileTest(unittest.TestCase):
    """`xun.core.config` loads the working directory's `.env` at import, which is where
    `DEFAULTS` and every home dir are resolved; an exported variable still wins."""

    src_dir = str(Path(__file__).resolve().parent.parent / "src")

    def run_xun(self, dotenv: str, probe: str, env_extra: dict[str, str] | None = None) -> str:
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, ".env").write_text(dotenv, encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("XUN_", "_XUN_"))}
            env["PYTHONPATH"] = self.src_dir + os.pathsep + env.get("PYTHONPATH", "")
            env.update(env_extra or {})
            result = subprocess.run([sys.executable, "-c", probe], cwd=temp, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

    def test_file_reaches_home_dir_and_defaults(self) -> None:
        probe = ("from xun.core.config import get_home_dir, DEFAULTS; "
                 "print(get_home_dir(), DEFAULTS.subagent_max_depth)")
        out = self.run_xun("XUN_HOME=/from/dotenv\n_XUN_SUBAGENT_MAX_DEPTH=5\n", probe)
        self.assertEqual(out, "/from/dotenv 5")

    def test_environment_wins_over_the_file(self) -> None:
        probe = "from xun.core.config import get_home_dir; print(get_home_dir())"
        out = self.run_xun("XUN_HOME=/from/dotenv\n", probe, {"XUN_HOME": "/from/env"})
        self.assertEqual(out, "/from/env")


if __name__ == "__main__":
    unittest.main()
