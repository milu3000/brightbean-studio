"""Quote event isolation tests; actual DOM checks use the separate Chrome runner."""

import shutil
import subprocess
from pathlib import Path


def test_quote_controls():
    node = shutil.which("node")
    assert node, "Node.js is required for quote checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("inbox_quote_test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
