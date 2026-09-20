# Imported by unittest discovery before any test module; isolates the suite from
# the developer's real XUN_HOME (config, store, and especially auto-run extensions).
import os
import tempfile

os.environ.setdefault("XUN_HOME", tempfile.mkdtemp(prefix="xun-test-home-"))
