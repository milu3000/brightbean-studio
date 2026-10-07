"""Run the real notification controller from the base template in a DOM stub."""

import shutil
import subprocess
from pathlib import Path


def test_notification_refresh_controller():
    node = shutil.which("node")
    assert node, "Node.js is required for the notification controller regressions"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("notification_refresh_test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
