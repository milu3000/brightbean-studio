"""Exercise unified conversation scrolling and transient content in browser timezones."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("browser_timezone", ["UTC", "America/Los_Angeles"])
def test_native_thread_snapshot_preserves_unsaved_work(browser_timezone):
    node = shutil.which("node")
    assert node, "Node.js is required for inbox native thread tests"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("inbox_native_thread_test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "TZ": browser_timezone},
    )
    assert result.returncode == 0, result.stdout + result.stderr
