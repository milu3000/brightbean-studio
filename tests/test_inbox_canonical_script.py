"""Logic-only controller regressions; the real Chromium test remains a separate gate."""

import shutil
import subprocess
from pathlib import Path


def test_canonical_controller():
    node = shutil.which("node")
    assert node, "Node.js is required for inbox controller checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("inbox_canonical_test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
