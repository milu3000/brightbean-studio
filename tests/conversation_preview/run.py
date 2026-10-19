"""Run with: python -m tests.conversation_preview.run [--port 8765]."""

import argparse
import os
import re
import secrets
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from .guard import MARKER, check_database


def _deny_network(*args, **kwargs):
    raise RuntimeError("Outbound network is disabled in the synthetic preview")


def export_pages(destination, password):
    """Export the actual authenticated HTTP rendering, without cookies/cursors."""
    from django.test import Client
    from django.test.utils import setup_test_environment

    from .fixtures import DEMO_EMAIL, SCENARIOS

    destination.mkdir(parents=True, exist_ok=False)
    setup_test_environment()
    client = Client()
    if not client.login(username=DEMO_EMAIL, password=password):
        raise RuntimeError("Synthetic export authentication failed")
    for key, _label, _description in SCENARIOS:
        cursor = None
        page_number = 1
        while True:
            path = f"/scenario/{key}/"
            response = client.get(path + ("?" + urlencode({"cursor": cursor}) if cursor else ""))
            if response.status_code != 200:
                raise RuntimeError(f"Synthetic render failed: {key} page {page_number}: {response.status_code}")
            html = response.content.decode()
            cursor = response.context["next_cursor"]
            for other_key, _other_label, _other_description in SCENARIOS:
                html = html.replace(f"/scenario/{other_key}/", f"{other_key}.html")
            next_name = f"{key}-{page_number + 1}.html"
            html = re.sub(r'href="\?cursor=[^"]+"', f'href="{next_name}"', html)
            html = html.replace("本地合成資料 · 只讀預覽", "本地合成資料 · 靜態匯出")
            if "?cursor=" in html or "/scenario/" in html or password in html or "csrfmiddlewaretoken" in html:
                raise RuntimeError("Refusing export containing runtime navigation or login data")
            name = f"{key}.html" if page_number == 1 else f"{key}-{page_number}.html"
            (destination / name).write_text(html)
            if cursor is None:
                break
            page_number += 1
    (destination / "index.html").write_text((destination / "burst.html").read_text())
    print(f"Static synthetic HTML exported to {destination}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--export", type=Path, help="New directory for self-contained HTML pages")
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use an unprivileged port between 1024 and 65535")
    if args.export_only and args.export is None:
        parser.error("--export-only requires --export")
    if os.environ.get("DATABASE_URL") or os.environ.get("BRIGHTBEAN_SYNTHETIC_ROOT"):
        parser.error("Unset DATABASE_URL and BRIGHTBEAN_SYNTHETIC_ROOT; this launcher creates its own new temporary DB")
    if os.environ.get("DJANGO_SETTINGS_MODULE") not in {None, "tests.conversation_preview.settings"}:
        parser.error("Unset DJANGO_SETTINGS_MODULE; production/development settings are never accepted")
    root = Path(tempfile.mkdtemp(prefix="brightbean-synthetic-preview-"))
    (root / ".synthetic-only").write_text(MARKER)
    os.environ["BRIGHTBEAN_SYNTHETIC_ROOT"] = str(root)
    os.environ["BRIGHTBEAN_SYNTHETIC_SECRET"] = secrets.token_urlsafe(48)
    os.environ["DJANGO_SETTINGS_MODULE"] = "tests.conversation_preview.settings"
    # No .env or deployment settings are imported. Prevent even an accidental
    # provider/telemetry call from this process. Accepted HTTP sockets still work.
    socket.socket.connect = _deny_network
    socket.socket.connect_ex = _deny_network
    socket.create_connection = _deny_network
    import django
    from django.conf import settings
    from django.core.management import call_command

    django.setup()
    check_database()
    if (root / "synthetic.sqlite3").exists():
        raise RuntimeError("Refusing to migrate an existing database")
    call_command("migrate", interactive=False, verbosity=0)
    from .fixtures import DEMO_EMAIL, enrollments, seed_synthetic_graph

    password = "DEMO-LOCAL-ONLY-" + secrets.token_urlsafe(12)
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = enrollments()
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = enrollments()
    seed_synthetic_graph(password)
    if args.export:
        export_pages(args.export, password)
    if args.export_only:
        return
    print(f"\nSynthetic-only preview: http://127.0.0.1:{args.port}/", flush=True)
    print(f"Demo email: {DEMO_EMAIL}\nDemo password (only this temporary DB): {password}", flush=True)
    print(f"Disposable synthetic DB: {root}\nNo worker is started. Stop with Ctrl+C.\n", flush=True)
    call_command("runserver", f"127.0.0.1:{args.port}", use_reloader=False)


if __name__ == "__main__":
    main()
