"""Refuse non-temporary databases; the launcher never accepts a database URL."""

import os
import tempfile
from pathlib import Path

MARKER = "Brightbean anonymous synthetic preview only\n"


def preview_root():
    raw = os.environ.get("BRIGHTBEAN_SYNTHETIC_ROOT", "")
    if not raw:
        raise RuntimeError("Start with python -m tests.conversation_preview.run")
    root = Path(raw).resolve()
    if (
        root.parent != Path(tempfile.gettempdir()).resolve()
        or not root.name.startswith("brightbean-synthetic-preview-")
        or not root.is_dir()
        or (root / ".synthetic-only").read_text() != MARKER
    ):
        raise RuntimeError("Refusing a database outside the disposable synthetic preview directory")
    return root


def check_database():
    from django.db import connection

    root = preview_root()
    if connection.vendor != "sqlite" or Path(connection.settings_dict["NAME"]).resolve() != root / "synthetic.sqlite3":
        raise RuntimeError("Refusing non-synthetic database")
