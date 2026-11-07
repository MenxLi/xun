import os
import unittest
import tempfile
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from xun.core.config import get_home_dir, env_defaults, load_env


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
    """Only entrypoints load `.env`; importing xun never touches the environment."""

    src_dir = str(Path(__file__).resolve().parent.parent / "src")

    def run_xun(self, probe: str) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("XUN_", "_XUN_"))}
        env["PYTHONPATH"] = self.src_dir + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run([sys.executable, "-c", probe], cwd=self.temp.name, env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        Path(self.temp.name, ".env").write_text("XUN_HOME=/from/dotenv\n_XUN_SUBAGENT_MAX_DEPTH=5\n",
                                                encoding="utf-8")

    def test_importing_xun_does_not_load_the_file(self) -> None:
        probe = "import os; import xun.core.config; print(os.environ.get('XUN_HOME'))"
        self.assertEqual(self.run_xun(probe), "None")

    def test_load_env_finds_the_cwd_file_and_reaches_every_reader(self) -> None:
        probe = ("from xun.core.config import load_env, get_home_dir, env_defaults; "
                 "load_env(); print(get_home_dir(), env_defaults().subagent_max_depth)")
        self.assertEqual(self.run_xun(probe), "/from/dotenv 5")

    def test_load_env_explicit_path(self) -> None:
        dotenv = Path(self.temp.name, ".env")
        with patch.dict(os.environ, {}, clear=True):
            load_env(dotenv)
            self.assertEqual(get_home_dir(), Path("/from/dotenv"))
            self.assertEqual(env_defaults().subagent_max_depth, 5)

    def test_environment_wins_over_the_file(self) -> None:
        with patch.dict(os.environ, {"XUN_HOME": "/from/env"}):
            load_env(Path(self.temp.name, ".env"))
            self.assertEqual(get_home_dir(), Path("/from/env"))


if __name__ == "__main__":
    unittest.main()
