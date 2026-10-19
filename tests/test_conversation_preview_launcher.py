"""The demo cannot accidentally select a deployed database or route."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from django.urls import Resolver404, resolve

from tests.conversation_preview.guard import MARKER, preview_root


def test_guard_requires_new_marked_temporary_directory(monkeypatch):
    monkeypatch.delenv("BRIGHTBEAN_SYNTHETIC_ROOT", raising=False)
    with pytest.raises(RuntimeError):
        preview_root()
    with tempfile.TemporaryDirectory(prefix="brightbean-synthetic-preview-") as directory:
        root = Path(directory)
        monkeypatch.setenv("BRIGHTBEAN_SYNTHETIC_ROOT", directory)
        (root / ".synthetic-only").write_text("not a preview")
        with pytest.raises(RuntimeError):
            preview_root()
        (root / ".synthetic-only").write_text(MARKER)
        assert preview_root() == root.resolve()
    monkeypatch.setenv("BRIGHTBEAN_SYNTHETIC_ROOT", str(Path.cwd()))
    with pytest.raises(RuntimeError):
        preview_root()


@pytest.mark.parametrize(
    "environment",
    [
        {"DATABASE_URL": "postgres://never-connect.invalid/synthetic-guard-check"},
        {"BRIGHTBEAN_SYNTHETIC_ROOT": "/tmp/existing-unknown-db"},
        {"DJANGO_SETTINGS_MODULE": "config.settings.production"},
    ],
)
def test_launcher_rejects_existing_database_environment_before_django_setup(environment):
    env = os.environ.copy()
    for name in ("DATABASE_URL", "BRIGHTBEAN_SYNTHETIC_ROOT", "DJANGO_SETTINGS_MODULE"):
        env.pop(name, None)
    env.update(environment)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.conversation_preview.run",
            "--export-only",
            "--export",
            "/tmp/never-created-by-rejected-preview",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert "Unset" in result.stderr
    assert "Traceback" not in result.stderr


def test_production_urlconf_has_no_preview_routes():
    with pytest.raises(Resolver404):
        resolve("/scenario/burst/", urlconf="config.urls")
