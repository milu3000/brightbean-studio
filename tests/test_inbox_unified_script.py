"""Offline mixed-controller event checks; real DOM has its own Chromium gate."""

import shutil
import subprocess
from pathlib import Path


def test_inbox_unified_events():
    node = shutil.which("node")
    assert node, "Node.js is required for unified inbox checks"
    result = subprocess.run(
        [
            node,
            "--test",
            str(Path(__file__).with_name("inbox_unified_test.cjs")),
            str(Path(__file__).with_name("inbox_history_race_test.cjs")),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
