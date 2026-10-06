"""Run transient native snapshot interruption checks in the ordinary suite."""

import shutil
import subprocess
from pathlib import Path


def test_native_thread_snapshot_preserves_unsaved_work():
    node = shutil.which("node")
    assert node, "Node.js is required for inbox native thread tests"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("inbox_native_thread_test.cjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
