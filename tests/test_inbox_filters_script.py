"""Offline event checks; this is not a real-browser verification."""

import shutil
import subprocess
from pathlib import Path


def test_inbox_filter_controls():
    node = shutil.which("node")
    assert node, "Node.js is required for inbox filter checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("inbox_filters_test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
