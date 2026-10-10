"""Render the real unified application shell and compiled CSS in offline Chromium."""

import json
import os
import re
import subprocess
from datetime import timedelta
from html import unescape
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.inbox import canonical_views
from apps.inbox.models import ConversationMessage, InboxMessage
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
    ConversationMessage.objects.filter(conversation=owner.conversation).update(sender_name="9911887766554433")
    for message in ConversationMessage.objects.filter(conversation=owner.conversation):
        proof(owner, message)
    accounts = []
    for index in range(10):
        accounts.append(
            SocialAccount.objects.create(
                workspace=owner.account.workspace,
                platform="facebook" if index % 2 else "instagram_login",
                account_platform_id=f"synthetic-shell-account-{index}",
                account_name=f"Synthetic brand {index // 2}",
                account_handle=f"synthetic.brand.{index // 2}",
            )
        )
    historical = SocialAccount.objects.create(
        workspace=owner.account.workspace,
        platform="threads",
        account_platform_id="synthetic-historic-account",
        account_name="Synthetic historical brand",
        account_handle="historic.brand",
    )
    legacy = []
    for index, (kind, account, name, handle) in enumerate(
        (
            ("dm", accounts[1], "合成訪客", "9988776655443322"),
            ("comment", accounts[0], "synthetic.visitor", "9911223344556677"),
            ("mention", accounts[1], "Synthetic Reader", "synthetic.reader"),
            ("review", historical, "Historic reviewer", "reviewer"),
        )
    ):
        legacy.append(
            InboxMessage.objects.create(
                workspace=owner.account.workspace,
                social_account=account,
                platform_message_id=f"synthetic-shell-{kind}",
                message_type=kind,
                sender_name=name,
                sender_handle=handle,
                body=f"Synthetic {kind} message",
                received_at=owner.clock.now - timedelta(minutes=index + 1),
                extra={"conversation_id": "synthetic-legacy-thread"} if kind == "dm" else {},
            )
        )
    for index in range(55):
        InboxMessage.objects.create(
            workspace=owner.account.workspace,
            social_account=accounts[0],
            platform_message_id=f"synthetic-older-comment-{index}",
            message_type="comment",
            sender_name=f"Synthetic older sender {index}",
            body=f"Older saved comment {index}",
            received_at=owner.clock.now - timedelta(days=index + 1),
        )
    routes, list_routes, history_routes = {}, {}, {}
    feed = reverse("inbox:feed", kwargs={"workspace_id": owner.account.workspace_id})
    detail = reverse(
        "inbox:conversation_detail",
        kwargs={"workspace_id": owner.account.workspace_id, "conversation_id": owner.conversation.pk},
    )

    captured_lists = {}

    def capture_url(url):
        pending = [(url, frozenset())]
        while pending:
            current, ancestors = pending.pop(0)
            assert current not in ancestors, "Fixture pagination must not cycle"
            if current in captured_lists:
                continue
            assert len(captured_lists) < 128, "Fixture pagination must remain bounded"
            response = client.get(current, HTTP_HX_REQUEST="true")
            assert response.status_code == 200, (current, response.content[:500])
            list_routes[current] = response.content.decode()
            restored = client.get(current, HTTP_HX_REQUEST="true", HTTP_HX_HISTORY_RESTORE_REQUEST="true")
            assert restored.status_code == 200, (current, restored.content[:500])
            history_routes[current] = restored.content.decode()
            captured_lists[current] = response
            # Each render can issue a different timestamp-signed cursor even for
            # the same snapshot. Export every exact emitted link, not an alias.
            for rendered in (response, restored):
                next_url = rendered.context["unified_next_url"]
                if next_url:
                    pending.append((next_url, ancestors | {current}))
        return captured_lists[url]

    def capture_list(params=None):
        return capture_url(feed + ("?" + urlencode(params) if params else ""))

    with (
        patch("apps.inbox.native_thread_reads.read_native_thread") as native,
        patch("apps.inbox.services._dispatch_to_platform") as provider,
    ):
        response = client.get(feed)
        assert response.status_code == 200, response.content[:500]
        routes[feed] = response.content.decode()
        first_rows = [row["id"] for row in response.context["unified_rows"]]
        next_url = response.context["unified_next_url"]
        assert next_url, "Real pagination is required by the shell fixture"
        next_response = capture_url(next_url)
        next_rows = [row["id"] for row in next_response.context["unified_rows"]]
        assert first_rows and next_rows and not set(first_rows).intersection(next_rows)
        for domain in ("all", "dm", "comment", "mention", "review"):
            capture_list({"domain": domain})
        capture_list()
        for params in (
            {"domain": "all", "platform": "facebook"},
            {"domain": "all", "account": str(accounts[0].pk)},
            {"domain": "all", "q": "synthetic.visitor"},
            {"q": "synthetic.visitor", "domain": "comment"},
            {"domain": "comment", "account": str(accounts[0].pk)},
            {"domain": "comment", "account": str(accounts[0].pk), "q": "synthetic.visitor"},
            {"domain": "dm", "account": str(owner.account.pk)},
        ):
            capture_list(params)
        # Detail can mark read in the isolated DB, so export list snapshots first.
        for url, headers in ((detail, {"HTTP_HX_REQUEST": "true"}), (detail + "?standalone=1", {})):
            response = client.get(url, **headers)
            assert response.status_code == 200, response.content[:500]
            routes[url] = response.content.decode()
        legacy_threads = []
        for message in legacy:
            url = reverse(
                "inbox:message_detail",
                kwargs={"workspace_id": owner.account.workspace_id, "message_id": message.pk},
            )
            response = client.get(url, HTTP_HX_REQUEST="true")
            assert response.status_code == 200, response.content[:500]
            routes[url] = response.content.decode()
            legacy_threads.append({"id": str(message.pk), "detail": url, "kind": message.message_type})
        history_response = client.get(detail, {"fragment": "history"}, HTTP_HX_REQUEST="true")
        assert history_response.status_code == 200
        with override_settings(INBOX_CANONICAL_READ_ENABLED=False):
            old_legacy = client.get(feed + "?domain=comment")
            assert old_legacy.status_code == 200
        old_request = client.get(feed, {"domain": "dm", "account": str(owner.account.pk)}).wsgi_request
        old_canonical = canonical_views.feed(old_request, owner.account.workspace)
        assert old_canonical.status_code == 200
        routes[feed + "?shell-negative=legacy"] = old_legacy.content.decode()
        routes[feed + "?shell-negative=duplicate"] = old_canonical.content.decode()
        native.assert_not_called()
        provider.assert_not_called()
    assets = {
        "https://canonical.test/static/css/dist/styles.css": {"body": css.read_text(), "type": "text/css"},
        "https://canonical.test/static/css/inbox-unified.css": {
            "body": (ROOT / "static/css/inbox-unified.css").read_text(),
            "type": "text/css",
        },
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
        "listHtml": list_routes[feed],
        "listRoutes": list_routes,
        "historyRoutes": history_routes,
        "assets": assets,
        "fullShell": True,
        "legacyAccountIds": [str(account.pk) for account in accounts] + [str(historical.pk)],
        "commentAccount": str(accounts[0].pk),
        "canonicalAccount": str(owner.account.pk),
        "legacyThreads": legacy_threads,
        "negativeRoutes": [feed + "?shell-negative=legacy", feed + "?shell-negative=duplicate"],
        "firstRows": first_rows,
        "nextRows": next_rows,
        "contentRoutes": {},
        "postResponses": {
            reverse(
                "inbox:native_thread_refresh",
                kwargs={"workspace_id": owner.account.workspace_id, "message_id": legacy[0].pk},
            ): {"status": "unavailable", "reason_code": "read_failed", "anchor_message_id": str(legacy[0].pk)},
        },
        "threads": [
            {
                "id": str(owner.conversation.pk),
                "detail": detail,
                "initialHistory": history_response.content.decode(),
                "read": reverse(
                    "inbox:conversation_read_ack",
                    kwargs={"workspace_id": owner.account.workspace_id, "conversation_id": owner.conversation.pk},
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
    assert html.count("data-inbox-account") == 1
    assert "Switch account" not in html
    form = re.search(r'<form[^>]+id="inbox-filters"[^>]*>', html)
    assert form and 'hx-swap="innerHTML settle:0ms"' in form.group()
    assert 'hx-replace-url="true"' in form.group(), "Mutation refresh must replace its stale page URL on success"
    list_links = re.findall(r'<a[^>]+hx-target="#inbox-list-content"[^>]*>', html)
    assert list_links and all('hx-swap="innerHTML settle:0ms"' in link for link in list_links)
    assert 'data-unified-domain="all" aria-current="page"' in html
    assert 'data-message-source="canonical"' in html and 'data-message-source="legacy"' in html
    assert all(f'data-message-type="{kind}"' in html for kind in ("dm", "comment", "mention", "review"))
    assert "@9911223344556677" not in html and "@9988776655443322" not in html
    assert "Unknown sender" in html
    assert "Facebook" in html and "Instagram" in html
    assert manifest["firstRows"] and manifest["nextRows"]
    # Signed cursor timestamps can differ between initial render and refresh.
    # Every exact URL emitted by either response must exist in the fixture;
    # never accept arbitrary cursor aliases to hide an unknown browser request.
    for html in [
        manifest["routes"][manifest["feed"]],
        *manifest["listRoutes"].values(),
        *manifest["historyRoutes"].values(),
    ]:
        for anchor in re.findall(r"<a[^>]+data-unified-next[^>]*>", html):
            target = re.search(r'href="([^"]+)"', anchor)
            assert target and unescape(target.group(1)) in manifest["listRoutes"], anchor
            assert unescape(target.group(1)) in manifest["historyRoutes"], anchor


@pytest.mark.django_db(transaction=True)
def test_history_restore_returns_shell_without_enabling_history_cache(shell_export):
    _destination, manifest = shell_export
    for url, html in manifest["historyRoutes"].items():
        assert "data-unified-shell" in html, url
        assert 'id="inbox-list-content" hx-history-elt' in html, url
        assert 'hx-history="false"' in html, url
        assert "sidebar-initial" in html, url
        assert "data-unified-shell" not in manifest["listRoutes"][url], url


@pytest.mark.django_db(transaction=True)
def test_canonical_actual_shell_chromium(request):
    node, binary = require_browser()
    destination, _manifest = request.getfixturevalue("shell_export")
    result = subprocess.run(
        [node, str(ROOT / "tests/inbox_shell_browser.cjs"), "--browser", binary, "--fixtures", str(destination)],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACTUAL SHELL CHROMIUM PASSED" in result.stdout
