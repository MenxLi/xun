import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
