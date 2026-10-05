"""Run the dependency-free JS behavior checks as part of the ordinary CI suite."""

import shutil
import subprocess
from pathlib import Path


def test_inbox_media_preview_failure_behavior():
    node = shutil.which("node")
    assert node, "Node.js is required for inbox media display tests"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("inbox_media_test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
