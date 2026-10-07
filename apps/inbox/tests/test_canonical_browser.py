"""Real Chromium integration of rendered canonical fragments and bundled JS.

The browser gets only synthetic Django test-database responses over CDP Fetch.
There is no HTTP server, real account, provider call, browser download, or live
inbox access. The viewport shell replaces base.html because pytest does not
build Tailwind; canonical layout rules and all inbox templates/scripts are real.
"""

import json
import os
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import (
    ConversationMessage,
    ConversationObservationState,
    ConversationSyncIdentity,
    ConversationWorkState,
    InboxConversation,
)
from apps.inbox.tests.test_canonical_reads_rebuilt import proof
from apps.inbox.tests.test_owned_composer_bridge import clock as _clock
from apps.inbox.tests.test_owned_composer_bridge import owner as _owner

owner, clock = _owner, _clock
ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "tests" / "inbox_canonical_browser.cjs"
LATE_IMAGE = "https://synthetic-browser-fixture.fbcdn.net/fixture/late-image.svg"
BASE = """{% load static %}<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}html,body{height:100%;margin:0;font:14px/1.5 sans-serif}
body{--primary:#ea580c;--surface-1:white}main{height:100%;padding:.75rem}
[hidden],[x-cloak]{display:none!important}button,a,select{cursor:pointer}
button,input,select,textarea{font:inherit}button{padding:4px 8px}
textarea{display:block;width:100%;resize:none}.flex{display:flex}.flex-wrap{flex-wrap:wrap}
.items-center{align-items:center}.justify-between{justify-content:space-between}
.flex-shrink-0{flex-shrink:0}.gap-2{gap:.5rem}.gap-3{gap:.75rem}
.block{display:block}.border-b{border-bottom:1px solid #e7e5e4}
.px-4{padding-left:1rem;padding-right:1rem}.py-3{padding-top:.75rem;padding-bottom:.75rem}
.p-3{padding:.75rem}.px-5{padding-left:1.25rem;padding-right:1.25rem}
.mt-2{margin-top:.5rem}.ml-auto{margin-left:auto}.whitespace-pre-wrap{white-space:pre-wrap}
.max-w-full{max-width:100%}.max-h-64{max-height:16rem}.sr-only{position:absolute;clip:rect(0,0,0,0)}
@media(min-width:640px){main{padding:1rem}}
@media(min-width:1024px){main{padding:1.5rem}}
@media(min-width:768px){[data-canonical-back]{display:none}}
</style>{% block extra_head %}{% endblock %}</head>
<body><main x-data="{fixtureReady:true}" :data-alpine-ready="fixtureReady">
{% block content %}{% endblock %}</main>
<script src="{% static 'js/htmx.min.js' %}"></script>
<script src="{% static 'js/alpine.min.js' %}" defer></script></body></html>"""


def browser_binary():
    explicit = os.environ.get("CHROME_BIN") or os.environ.get("CHROMIUM_BIN")
    if explicit:
        return shutil.which(explicit)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(mac) if mac.is_file() else None


def require_browser():
    """CI must run the DOM test; only explicit local limitations may skip it."""
    blocked = os.environ.get("BRIGHTBEAN_BROWSER_BLOCKED_REASON")
    node, binary = shutil.which("node"), browser_binary()
    reason = blocked or (
        "Node.js is unavailable" if not node else "Chrome/Chromium is unavailable" if not binary else ""
    )
    if reason:
        if os.environ.get("CI"):
            pytest.fail(f"Required Chromium regression could not run: {reason}")
        pytest.skip(f"Chromium DOM assertions NEVER RAN: {reason}")
    return node, binary


@pytest.fixture
def browser_export(client, owner, settings, tmp_path):
    composer = owner
    settings.INBOX_CANONICAL_READ_ENABLED = True
    settings.TEMPLATES = [
        {
            **settings.TEMPLATES[0],
            "APP_DIRS": False,
            "OPTIONS": {
                **settings.TEMPLATES[0]["OPTIONS"],
                "loaders": [
                    ("django.template.loaders.locmem.Loader", {"base.html": BASE}),
                    "django.template.loaders.filesystem.Loader",
                    "django.template.loaders.app_directories.Loader",
                ],
            },
        }
    ]
    client.force_login(composer.user)
    other = InboxConversation.objects.create(
        workspace=composer.account.workspace,
        social_account=composer.account,
        platform=composer.account.platform,
        platform_conversation_id="browser-fixture-second-thread",
        peer_id="browser-fixture-other-peer",
        identity_kind="platform",
        conversation_type="direct",
        workflow_state="needs_action",
    )
    now = timezone.now()
    rows = []
    for thread, count in ((composer.conversation, 540), (other, 40)):
        for index in range(count):
            rows.append(
                ConversationMessage(
                    workspace=composer.account.workspace,
                    social_account=composer.account,
                    platform=composer.account.platform,
                    conversation=thread,
                    platform_message_id=f"browser-fixture-{thread.pk}-{index}",
                    sender_id=thread.peer_id,
                    recipient_id=composer.account.account_platform_id,
                    sender_name="Synthetic customer",
                    direction="inbound",
                    conversation_attribution="platform",
                    conversation_type="direct",
                    classification_reason="participants_pair",
                    delivery_status="observed",
                    body=f"Synthetic browser message {index:04d} " + "Safe fixture text. " * 3,
                    occurred_at=now - timedelta(minutes=index + 2),
                    incoming_generation=1,
                    attachments=[{"type": "image", "url": LATE_IMAGE}]
                    if thread == composer.conversation and index == 35
                    else [],
                )
            )
    ConversationMessage.objects.bulk_create(rows)
    body_row, media_row, retained_row = rows[:3]
    body_row.body = "Browser full text START " + "B" * 4400 + " <script>fixtureOnly()</script> END"
    body_row.save(update_fields=["body"])
    media_row.attachments = [
        {
            "type": "share",
            "title": f"Synthetic attachment {index}",
            "url": f"https://example.com/fixture/link-{index}",
        }
        for index in range(7)
    ]
    media_row.save(update_fields=["attachments"])
    retained_body = "PRIVATE SYNTHETIC RETAINED START " + "R" * 4100 + " RETAINED END"
    retained_media = [
        {
            "type": "share",
            "title": f"Retained attachment {index}",
            "url": f"https://example.com/fixture/retained-{index}",
        }
        for index in range(4)
    ]
    connection = proof(
        composer, retained_row, withdrawn_at=now, retained_body=retained_body, retained_attachments=retained_media
    )
    ConversationSyncIdentity.objects.create(
        conversation=other, connection=connection, connection_generation=connection.generation
    )
    ConversationObservationState.objects.bulk_create(
        [
            ConversationObservationState(
                message=message,
                connection_generation=connection.generation,
                content_fingerprint="synthetic-browser-capture",
                last_observed_at=now,
                expires_at=now + timedelta(days=150),
            )
            for message in ConversationMessage.objects.filter(observation_state__isnull=True)
        ]
    )
    initial_count = ConversationMessage.objects.filter(conversation=composer.conversation).count()

    def url(name, thread=composer.conversation):
        kwargs = {"workspace_id": composer.account.workspace_id}
        if name != "feed":
            kwargs["conversation_id"] = thread.pk
        return reverse(f"inbox:{name}", kwargs=kwargs)

    routes = {}

    def capture(path, *, htmx=True):
        response = client.get(path, **({"HTTP_HX_REQUEST": "true"} if htmx else {}))
        assert response.status_code == 200, (path, response.status_code, response.content[:500])
        routes[path] = response.content.decode()
        return response

    def composer_fields(response):
        state = response.context["conversation_composer"]
        return {
            "observation": response.context["composer_observation_token"],
            "scope": state["scope_token"],
            "revision": str(state["composer_revision"]),
            "sendAllowed": response.context["send_availability"]["allowed"],
        }

    with (
        patch("apps.inbox.native_thread_reads.read_native_thread") as native,
        patch("apps.inbox.services._dispatch_to_platform") as provider,
    ):
        capture(url("feed"), htmx=False)
        list_response = client.get(url("feed"), HTTP_HX_REQUEST="true")
        assert list_response.status_code == 200
        list_html = list_response.content.decode()
        threads = []
        for thread in (composer.conversation, other):
            detail = capture(url("conversation_detail", thread))
            older = detail.context["canonical_older_url"]
            pages = 0
            while older:
                page = capture(older)
                older = page.context["canonical_older_url"]
                pages += 1
                assert pages <= 30, "Synthetic history pagination did not terminate"
            threads.append(
                {
                    "id": str(thread.pk),
                    "detail": url("conversation_detail", thread),
                    "read": url("conversation_read_ack", thread),
                    "send": url("conversation_send_reply", thread),
                    "save": url("conversation_save_draft", thread),
                    "initialIds": [item["id"] for item in detail.context["canonical_messages"]],
                    "composerFields": composer_fields(detail),
                    # Export the actual endpoint, including workspace-bound
                    # content URLs, view scope, header, and observation token.
                    "initialHistory": capture(
                        url("conversation_detail", thread) + "?fragment=history"
                    ).content.decode(),
                }
            )
        contents, viewer_cases = {}, []
        for message, kind, expected_body, expected_media in (
            (body_row, "body", body_row.body, []),
            (media_row, "attachments", "", media_row.attachments),
            (retained_row, "retained", retained_body, retained_media),
        ):
            content_url = reverse(
                "inbox:conversation_message_content",
                kwargs={
                    "workspace_id": composer.account.workspace_id,
                    "conversation_id": composer.conversation.pk,
                    "message_id": message.pk,
                },
            )
            pages, cursor = [], ""
            while True:
                response = client.post(content_url, {"kind": kind, "cursor": cursor})
                assert response.status_code == 200, response.content[:500]
                value = response.json()
                pages.append({"cursor": cursor, "payload": value})
                assert len(pages) <= 5, "Synthetic content continuation did not terminate"
                cursor = value.get("next_cursor") or ""
                if not cursor:
                    break
            contents[content_url] = {kind: pages}
            viewer_cases.append(
                {
                    "id": str(message.pk),
                    "path": content_url,
                    "kind": kind,
                    "body": expected_body,
                    "titles": [item["title"] for item in expected_media],
                }
            )
        # This is a new saved DB message, rendered through the actual read-only
        # history endpoint. CDP exposes it only when a browser scenario enables it.
        composer.clock.now += timedelta(seconds=1)
        fresh = ConversationMessage.objects.create(
            workspace=composer.account.workspace,
            social_account=composer.account,
            platform=composer.account.platform,
            conversation=composer.conversation,
            platform_message_id="browser-fixture-new-db-only-message",
            sender_id=composer.conversation.peer_id,
            recipient_id=composer.account.account_platform_id,
            sender_name="Synthetic customer",
            direction="inbound",
            conversation_attribution="platform",
            conversation_type="direct",
            classification_reason="participants_pair",
            delivery_status="observed",
            body="Fresh saved DB-only browser message",
            occurred_at=now + timedelta(seconds=1),
            incoming_generation=composer.conversation.incoming_generation + 1,
        )
        fresh_path = url("conversation_detail") + "?fragment=history"
        proof(composer, fresh)
        # A saved incoming observation also advances the conversation revision;
        # bulk fixture inserts deliberately bypass production ingestion hooks.
        InboxConversation.objects.filter(pk=composer.conversation.pk).update(
            revision=F("revision") + 1,
            incoming_generation=F("incoming_generation") + 1,
        )
        composer.conversation.refresh_from_db()
        ConversationWorkState.objects.filter(conversation=composer.conversation).update(
            conversation_revision=composer.conversation.revision, generation=F("generation") + 1
        )
        newest = capture(fresh_path)
        fresh_fields = composer_fields(newest)
        state = newest.context["conversation_composer"]
        saved = client.post(
            url("conversation_save_draft"),
            {
                "body": "Saved in another synthetic composer",
                "composer_action_nonce": state["action_nonce"],
                "composer_revision": state["composer_revision"],
                "composer_scope_token": state["scope_token"],
                "composer_observation_token": newest.context["composer_observation_token"],
            },
        )
        assert saved.status_code == 200 and "HX-Reply-Failed" not in saved, (
            saved.status_code,
            saved.context["composer_error"] if saved.context else saved.content[:500],
        )
        conflict = client.get(fresh_path, HTTP_HX_REQUEST="true")
        assert conflict.status_code == 200
        conflict_fields = composer_fields(conflict)
        native.assert_not_called()
        provider.assert_not_called()
    manifest = {
        "origin": "https://canonical.test",
        "feed": url("feed"),
        "listHtml": list_html,
        "threads": threads,
        "routes": routes,
        "freshPath": fresh_path,
        "freshId": str(fresh.pk),
        "rowCount": initial_count,
        "contentRoutes": contents,
        "viewerCases": viewer_cases,
        "lateImageUrl": LATE_IMAGE,
        "freshComposerFields": fresh_fields,
        "conflictHistory": conflict.content.decode(),
        "conflictComposerFields": conflict_fields,
    }
    destination = tmp_path / "canonical-browser-fixtures.json"
    destination.write_text(json.dumps(manifest), encoding="utf-8")
    return destination, manifest


@pytest.mark.django_db(transaction=True)
def test_canonical_browser_fixture_export(browser_export):
    """Exercise the real synthetic export even when the DOM gate is blocked."""
    destination, manifest = browser_export
    assert len(manifest["routes"]) >= 22
    assert "htmx.min.js" in manifest["routes"][manifest["feed"]]
    assert "alpine.min.js" in manifest["routes"][manifest["feed"]]
    assert "inbox-message-details.js" in manifest["routes"][manifest["feed"]]
    assert "data-canonical-panel" in manifest["routes"][manifest["threads"][0]["detail"]]
    assert manifest["freshId"] in manifest["routes"][manifest["freshPath"]]
    assert all("PRIVATE SYNTHETIC RETAINED" not in html for html in manifest["routes"].values())
    panel = manifest["routes"][manifest["threads"][0]["detail"]]
    for item in manifest["viewerCases"]:
        article = re.search(
            rf'<article[^>]*data-canonical-message="{re.escape(item["id"])}".*?</article>', panel, re.DOTALL
        )
        assert article and f'data-inbox-detail-open="{item["kind"]}"' in article.group()
    assert f'src="{LATE_IMAGE}"' in "".join(manifest["routes"].values())
    initial, fresh, conflict = (
        manifest["threads"][0]["composerFields"],
        manifest["freshComposerFields"],
        manifest["conflictComposerFields"],
    )
    assert initial["sendAllowed"] and fresh["sendAllowed"]
    assert initial["observation"] != fresh["observation"] and initial["scope"] != fresh["scope"]
    assert initial["revision"] == fresh["revision"] != conflict["revision"]
    node = shutil.which("node")
    assert node, "Node.js is required to validate the browser fixture contract"
    result = subprocess.run(
        [node, str(HARNESS), "--validate-fixtures", str(destination)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cdp_pipe_transport_unit_contract():
    node = shutil.which("node")
    assert node, "Node.js is required to validate the CDP pipe contract"
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "browser" / "cdp_test.cjs")],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db(transaction=True)
def test_canonical_real_chromium(request):
    node, binary = require_browser()
    destination, _manifest = request.getfixturevalue("browser_export")
    result = subprocess.run(
        [node, str(HARNESS), "--browser", binary, "--fixtures", str(destination)],
        capture_output=True,
        text=True,
        timeout=150,
        check=False,
    )
    # Launch failure, missing CDP, assertion failure, or a partial scenario never
    # becomes a pass or an implicit local skip.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHROMIUM DOM REGRESSIONS PASSED" in result.stdout


@pytest.mark.parametrize("reason", ["known sandbox socket EPERM", "missing browser"])
def test_browser_gate_cannot_skip_in_ci(monkeypatch, reason):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("BRIGHTBEAN_BROWSER_BLOCKED_REASON", reason)
    with pytest.raises(pytest.fail.Exception, match="Required Chromium regression could not run"):
        require_browser()


def test_missing_browser_is_a_ci_failure(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("BRIGHTBEAN_BROWSER_BLOCKED_REASON", raising=False)
    monkeypatch.setattr(f"{__name__}.browser_binary", lambda: None)
    with pytest.raises(pytest.fail.Exception, match="Required Chromium regression could not run"):
        require_browser()
