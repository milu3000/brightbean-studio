"""Normal inbox unassigned pages use the same persisted read-only source."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import (
    ConversationMessage,
    ConversationObservationState,
    ConversationReadState,
    InboxMessage,
    InboxReply,
)
from apps.inbox.tests.test_unassigned_reads import context as _context
from apps.inbox.tests.test_unassigned_reads import unassigned

context = _context
pytestmark = pytest.mark.django_db


@pytest.fixture
def session(client, context):
    client.force_login(context.user)
    return client


def url(context, name="unassigned_feed", message=None):
    values = {"workspace_id": context.account.workspace_id}
    if message:
        values["message_id"] = message.pk
    return reverse("inbox:" + name, kwargs=values)


def test_unassigned_visible_without_inventing_direct_thread_or_action(session, context):
    message = unassigned(context, body="Saved group text", occurred_at=None)
    assert "待辨識" in session.get(url(context, "feed")).content.decode()
    listing = session.get(url(context), {"q": "group text"})
    assert listing.status_code == 200 and "Saved group text" in listing.content.decode()
    assert "data-inbox-clear-search" in listing.content.decode()
    detail = session.get(url(context, "unassigned_detail", message))
    assert (
        detail.status_code == 200
        and "群組訊息" in detail.content.decode()
        and "Time unavailable" in detail.content.decode()
    )
    assert "data-inbox-reply-form" not in detail.content.decode() and "read-ack-token" not in detail.content.decode()
    assert (
        not InboxReply.objects.exists()
        and not InboxMessage.objects.exists()
        and not ConversationReadState.objects.exists()
    )


def test_unassigned_full_content_and_applied_expiry(session, context):
    message = unassigned(context, body="Actual body " * 500)
    content = url(context, "unassigned_message_content", message)
    assert 'data-inbox-detail-open="body"' in session.get(url(context, "unassigned_detail", message)).content.decode()
    output, cursor = "", ""
    while True:
        response = session.post(content, {"kind": "body", "cursor": cursor})
        assert response.status_code == 200
        value = response.json()
        output += value["body"]
        cursor = value["next_cursor"]
        assert value["conversation_id"] is None
        if not cursor:
            break
    assert output == message.body
    ConversationObservationState.objects.filter(message=message).update(expired_at=timezone.now())
    assert "Actual body" not in session.post(content, {"kind": "body"}).content.decode()


@pytest.mark.parametrize("proven", [True, False])
def test_proven_legacy_original_is_revealable_but_never_preloaded_or_returned_as_normal_body(session, context, proven):
    message = unassigned(context, body="", content_status="removed", is_deleted=True)
    state = message.observation_state
    state.withdrawn_at = timezone.now()
    state.expires_at = timezone.now() - timedelta(days=2)
    state.retained_legacy_body = "PROVEN PRIVATE LEGACY ORIGINAL"
    state.save()
    InboxMessage.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        platform_message_id=message.platform_message_id,
        message_type="dm",
        received_at=timezone.now(),
        extra={
            "canonical_retained_legacy_proof": {
                "message_id": str(message.pk),
                "generation": str(state.connection_generation if proven else uuid4()),
            }
        },
    )
    with CaptureQueriesContext(connection) as captured:
        response = session.get(url(context, "unassigned_detail", message))
    assert response.status_code == 200
    assert ('data-inbox-detail-open="retained"' in response.content.decode()) is proven
    assert "PROVEN PRIVATE" not in response.content.decode()
    selects = [
        query["sql"].split(" FROM ")[0] for query in captured.captured_queries if query["sql"].startswith("SELECT")
    ]
    assert not any('"retained_legacy_body"' in query for query in selects)
    normal = session.post(url(context, "unassigned_message_content", message), {"kind": "body"})
    assert "PROVEN PRIVATE" not in normal.content.decode()
    revealed = session.post(url(context, "unassigned_message_content", message), {"kind": "retained"})
    assert revealed.status_code == (200 if proven else 404)
    if proven:
        assert revealed.json()["body"] == "PROVEN PRIVATE LEGACY ORIGINAL"
        assert "expires_at" not in revealed.json()


def test_unassigned_assignment_race_or_revoked_access_holds_old_route(session, context, settings):
    message = unassigned(context)
    route = url(context, "unassigned_detail", message)
    ConversationMessage.objects.filter(pk=message.pk).update(conversation=context.conversation)
    assert session.get(route).status_code == 404
    ConversationMessage.objects.filter(pk=message.pk).update(conversation=None)
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    assert session.get(route).status_code in {404, 409}
