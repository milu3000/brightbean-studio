"""Offline shortcut regression checks; real browser execution is a separate gate."""

import shutil
import subprocess
from pathlib import Path


def test_inbox_composer_controls():
    node = shutil.which("node")
    assert node, "Node.js is required for inbox shortcut checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("inbox_composer_test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
