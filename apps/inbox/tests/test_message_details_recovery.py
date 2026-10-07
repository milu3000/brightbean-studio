"""Explicit human-only content access uses synthetic records and no provider."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import ConversationObservationState, ConversationReadState, InboxReply
from apps.inbox.tests.test_canonical_reads_rebuilt import proof
from apps.inbox.tests.test_canonical_session_recovery import composer as _composer
from apps.inbox.tests.test_canonical_session_recovery import record, route
from apps.inbox.tests.test_canonical_session_recovery import session as _session

composer = _composer
session = _session

pytestmark = pytest.mark.django_db(transaction=True)


def url(composer, message, conversation_id=None):
    return reverse(
        "inbox:conversation_message_content",
        kwargs={
            "workspace_id": composer.account.workspace_id,
            "conversation_id": conversation_id or composer.conversation.pk,
            "message_id": message.pk,
        },
    )


def withdrawn(composer, **kwargs):
    message = record(composer, 99)
    proof(composer, message, withdrawn_at=timezone.now(), retained_body="PRIVATE RETAINED TEXT", **kwargs)
    return message


def test_initial_page_only_exposes_capability_and_no_retained_content_is_selected(session, composer):
    message = withdrawn(composer)
    with CaptureQueriesContext(connection) as captured:
        response = session.get(route(composer))
    assert response.status_code == 200
    html = response.content.decode()
    assert 'data-inbox-detail-open="retained"' in html
    assert "PRIVATE RETAINED TEXT" not in html
    selects = [sql["sql"].split(" FROM ")[0] for sql in captured.captured_queries if sql["sql"].startswith("SELECT")]
    assert not any('"retained_body"' in sql or '"retained_attachments"' in sql for sql in selects)
    assert session.get(url(composer, message)).status_code == 405
    assert not ConversationReadState.objects.exists()


def test_explicit_view_is_bounded_preserves_body_and_never_fetches_media_or_changes_state(session, composer):
    message = withdrawn(composer)
    ConversationObservationState.objects.filter(message=message).update(
        retained_body="X" * 4501,
        retained_attachments=[{"type": "image", "url": "https://example.com/image"}] * 7,
    )
    output, media, cursor = "", [], None
    with patch("apps.inbox.native_thread_reads.read_native_thread") as provider:
        while True:
            response = session.post(url(composer, message), {"kind": "retained", "cursor": cursor or ""})
            assert response.status_code == 200 and "no-store" in response["Cache-Control"]
            data = response.json()
            assert len(data["body"]) <= 2000 and len(data["items"]) <= 3
            output += data["body"]
            media.extend(data["items"])
            cursor = data["next_cursor"]
            if not cursor:
                break
    assert output == "X" * 4501 and len(media) == 7
    provider.assert_not_called()
    assert not InboxReply.objects.exists() and not ConversationReadState.objects.exists()


@pytest.mark.parametrize("change", ["expired", "revoked", "cross_conversation", "rebound", "changed"])
def test_retained_cursor_and_scope_fail_closed(session, composer, settings, change):
    message = withdrawn(composer)
    ConversationObservationState.objects.filter(message=message).update(retained_body="X" * 2100)
    first = session.post(url(composer, message), {"kind": "retained"}).json()
    target = url(composer, message)
    if change == "expired":
        ConversationObservationState.objects.filter(message=message).update(expired_at=timezone.now())
    elif change == "revoked":
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    elif change == "cross_conversation":
        target = url(composer, message, uuid4())
    elif change == "rebound":
        composer.account.account_platform_id = "different-owner"
        composer.account.save(update_fields=["account_platform_id"])
    else:
        ConversationObservationState.objects.filter(message=message).update(retained_body="CHANGED")
    result = session.post(target, {"kind": "retained", "cursor": first["next_cursor"]})
    assert result.status_code in {404, 409} and "CHANGED" not in result.content.decode()


def test_planned_expiry_does_not_activate_an_unapproved_policy(session, composer):
    message = withdrawn(composer, expires_at=timezone.now() - timedelta(days=1))
    assert 'data-inbox-detail-open="retained"' in session.get(route(composer)).content.decode()
    response = session.post(url(composer, message), {"kind": "retained"})
    assert response.status_code == 200 and response.json()["body"] == "PRIVATE RETAINED TEXT"
    assert "expires" not in response.json()


def test_normal_continuation_cannot_reveal_retained_body(session, composer):
    message = withdrawn(composer)
    for kind in ["body", "attachments"]:
        response = session.post(url(composer, message), {"kind": kind})
        assert response.status_code == 200 and "PRIVATE RETAINED TEXT" not in response.content.decode()


def test_full_text_has_complete_explicit_continuation(session, composer):
    message = record(composer, 7, body="Complete body " * 450)
    response = session.get(route(composer))
    assert 'data-inbox-detail-open="body"' in response.content.decode()
    body, cursor = "", None
    while True:
        result = session.post(url(composer, message), {"kind": "body", "cursor": cursor or ""}).json()
        body += result["body"]
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert body == message.body


def test_composer_fragment_preserves_rendered_observation_even_if_background_read_is_newer(session, composer):
    from apps.inbox import canonical_reads
    from apps.inbox.tests.test_canonical_session_recovery import payload

    original = canonical_reads.read_conversation

    def newest(*args, **kwargs):
        result = original(*args, **kwargs)
        result["composer_observation_token"] = "newer-unseen-token"
        return result

    initial = session.get(route(composer))
    data = payload(initial)
    data["composer_observation_token"] = "actually-rendered-token"
    with patch.object(canonical_reads, "read_conversation", side_effect=newest):
        saved = session.post(route(composer, "conversation_save_draft"), data)
        latest = session.get(route(composer), {"fragment": "history"})
    assert saved.status_code == latest.status_code == 200
    assert saved.context["composer_observation_token"] == "actually-rendered-token"
    assert "newer-unseen-token" not in saved.content.decode()
    assert 'data-composer-observation-token="newer-unseen-token"' in latest.content.decode()
    assert "data-canonical-fresh-header" in latest.content.decode()
