"""Render the actual application shell and compiled CSS in offline Chromium."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.inbox.models import ConversationMessage
from apps.inbox.tests.test_canonical_browser import require_browser
from apps.inbox.tests.test_canonical_reads_rebuilt import proof
from apps.inbox.tests.test_owned_composer_bridge import clock as clock
from apps.inbox.tests.test_owned_composer_bridge import owner as owner
from apps.onboarding.models import OnboardingChecklist
from apps.social_accounts.models import SocialAccount

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def shell_export(client, owner, tmp_path):
    css = ROOT / "theme/static/css/dist/styles.css"
    assert css.is_file(), "Build actual CSS first: cd theme/static_src && npm ci && npm run build"
    client.force_login(owner.user)
    OnboardingChecklist.objects.create(user=owner.user, workspace=owner.account.workspace, is_dismissed=True)
    for message in ConversationMessage.objects.filter(conversation=owner.conversation):
        proof(owner, message)
    legacy_accounts = []
    for index in range(10):
        account = SocialAccount.objects.create(
            workspace=owner.account.workspace,
            platform="facebook" if index % 2 else "instagram_login",
            account_platform_id=f"synthetic-shell-account-{index}",
            account_name=f"Synthetic brand {index // 2}",
            account_handle=f"synthetic.brand.{index // 2}",
        )
        legacy_accounts.append(str(account.pk))
    routes = {}
    feed = reverse("inbox:feed", kwargs={"workspace_id": owner.account.workspace_id})
    detail = reverse(
        "inbox:conversation_detail",
        kwargs={"workspace_id": owner.account.workspace_id, "conversation_id": owner.conversation.pk},
    )
    with (
        patch("apps.inbox.native_thread_reads.read_native_thread") as native,
        patch("apps.inbox.services._dispatch_to_platform") as provider,
    ):
        for url, headers in ((feed, {}), (detail, {"HTTP_HX_REQUEST": "true"}), (detail + "?standalone=1", {})):
            response = client.get(url, **headers)
            assert response.status_code == 200, response.content[:500]
            routes[url] = response.content.decode()
        list_response = client.get(feed, HTTP_HX_REQUEST="true")
        history_response = client.get(detail, {"fragment": "history"}, HTTP_HX_REQUEST="true")
        assert list_response.status_code == history_response.status_code == 200
        native.assert_not_called()
        provider.assert_not_called()
    # Only unrelated optional CDN widgets are stubbed. Layout, base template,
    # compiled Tailwind, HTMX, Alpine and every inbox controller are real.
    assets = {
        "https://canonical.test/static/css/dist/styles.css": {"body": css.read_text(), "type": "text/css"},
        "https://canonical.test" + reverse("notifications:unread_count"): {
            "body": '{"count":0}',
            "type": "application/json",
        },
    }
    for package, filename, kind in (
        ("flatpickr@4.6.13", "dist/flatpickr.min.css", "text/css"),
        ("flatpickr@4.6.13", "dist/flatpickr.min.js", "text/javascript"),
        ("chart.js@4.4.6", "dist/chart.umd.min.js", "text/javascript"),
        ("sortablejs@1.15.6", "Sortable.min.js", "text/javascript"),
    ):
        assets[f"https://cdn.jsdelivr.net/npm/{package}/{filename}"] = {"body": "", "type": kind}
    for filename in ("favicon.ico", "favicon.svg", "favicon-96x96.png", "apple-touch-icon.png", "site.webmanifest"):
        assets[f"https://canonical.test/static/favicon/{filename}"] = {"body": "", "type": "text/plain"}
    manifest = {
        "origin": "https://canonical.test",
        "feed": feed,
        "routes": routes,
        "listHtml": list_response.content.decode(),
        "assets": assets,
        "fullShell": True,
        "legacyAccountIds": legacy_accounts,
        "contentRoutes": {},
        "threads": [
            {
                "id": str(owner.conversation.pk),
                "detail": detail,
                "initialHistory": history_response.content.decode(),
                "read": reverse(
                    "inbox:conversation_read_ack",
                    kwargs={
                        "workspace_id": owner.account.workspace_id,
                        "conversation_id": owner.conversation.pk,
                    },
                ),
            }
        ],
        "screenshotDirectory": os.environ.get("BRIGHTBEAN_BROWSER_ARTIFACTS", str(tmp_path)),
    }
    destination = tmp_path / "canonical-shell.json"
    destination.write_text(json.dumps(manifest))
    return destination, manifest


@pytest.mark.django_db(transaction=True)
def test_actual_shell_fixture_keeps_account_routes(shell_export):
    _destination, manifest = shell_export
    html = manifest["routes"][manifest["feed"]]
    assert "sidebar-initial" in html and "/static/css/dist/styles.css" in html
    assert html.count("Synthetic brand") >= 20
    assert "Facebook" in html and "Instagram" in html
    assert "data-canonical-account-switcher" in html
    assert 'name="read_source" value="canonical"' in html


@pytest.mark.django_db(transaction=True)
def test_canonical_actual_shell_chromium(request):
    node, binary = require_browser()
    destination, _manifest = request.getfixturevalue("shell_export")
    result = subprocess.run(
        [node, str(ROOT / "tests/inbox_shell_browser.cjs"), "--browser", binary, "--fixtures", str(destination)],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACTUAL SHELL CHROMIUM PASSED" in result.stdout
