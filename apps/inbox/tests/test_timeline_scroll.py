"""Saved history paging and join metadata cannot invent deliveries or mix scopes."""

from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core import signing
from django.utils import timezone

from apps.inbox import presentation
from apps.inbox.models import ConversationMessage, InboxConversation, InboxMessage, InboxReply, InternalNote
from apps.inbox.tests.test_conversation_presentation import detail_url, incoming
from apps.inbox.tests.test_conversation_presentation import owner_client as owner_client  # noqa: F401

pytestmark = pytest.mark.django_db


class TimelineMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.events = []
        self.pages = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("data-timeline-event") == "stored":
            self.events.append(attrs)
        if "data-stored-timeline-events" in attrs:
            self.pages.append(attrs)


def read_page(client, message, url=None):
    response = client.get(url or detail_url(message), HTTP_HX_REQUEST="true", HTTP_HX_TARGET="inbox-thread")
    assert response.status_code == 200
    return response, TimelineMarkup(response.content.decode())


def test_join_metadata_uses_exact_provider_ids_and_never_invents_sent_time(inbox_account, user):
    message = incoming(inbox_account, 'opaque:provider/"id&<>')
    delivered = InboxReply.objects.create(
        inbox_message=message,
        author=user,
        body="Delivered",
        status="sent",
        platform_reply_id="exact-outbound-id",
        sent_at=timezone.now(),
    )
    legacy = InboxReply.objects.create(
        inbox_message=message, body="Legacy", status="sent", platform_reply_id="unknown-time", sent_at=None
    )
    malformed = InboxReply.objects.create(
        inbox_message=message, body="No usable ID", status="sent", platform_reply_id=" id with spaces "
    )
    note = InternalNote.objects.create(inbox_message=message, author=user, body="Only internal")
    InboxReply.objects.create(inbox_message=message, body="Uncertain", status="unknown")
    page = presentation.timeline_page(presentation.stored_thread_messages(message), 1)
    events = {event["item"].pk: event for event in presentation.timeline_events(page.object_list)}

    assert events[message.pk]["platform_id"] == message.platform_message_id
    assert events[message.pk]["direction"] == "inbound"
    assert events[delivered.pk]["platform_id"] == "exact-outbound-id"
    assert events[delivered.pk]["occurred_at"] == delivered.sent_at.isoformat()
    assert events[legacy.pk]["occurred_at"] == ""
    assert events[malformed.pk]["platform_id"] == ""
    assert events[note.pk]["platform_id"] == ""
    assert events[note.pk]["direction"] == "internal"
    assert len(events) == 5


def test_scoped_join_markup_is_escaped_and_preserves_saved_content(owner_client, inbox_account, user):
    message = incoming(inbox_account, 'opaque"<fake>&', body="Saved incoming")
    reply = InboxReply.objects.create(
        inbox_message=message,
        author=user,
        body="Saved outgoing",
        status="sent",
        platform_reply_id="receipt-id",
        sent_at=timezone.now(),
    )
    response, markup = read_page(owner_client, message)
    assert len(markup.pages) == 1
    assert markup.pages[0]["data-timeline-anchor-id"] == str(message.pk)
    assert markup.pages[0]["data-history-complete"] == "true"
    events = {event["data-event-id"]: event for event in markup.events}
    assert events[f"incoming:{message.pk}"]["data-platform-message-id"] == message.platform_message_id
    assert events[f"reply:{reply.pk}"]["data-event-direction"] == "outbound"
    assert "mr-auto" in events[f"incoming:{message.pk}"]["class"]
    assert "ml-auto" in events[f"reply:{reply.pk}"]["class"]
    html = response.content.decode()
    assert html.count("Saved incoming") == html.count("Saved outgoing") == 1
    assert "<fake>" not in html
    assert "data-stored-event-body" in html and "data-stored-event-attachments" in html
    assert "inbox-reply-body" not in html


def test_keyset_older_pages_do_not_shift_when_a_new_message_arrives(owner_client, inbox_account):
    base = timezone.now() - timedelta(days=1)
    selected = incoming(inbox_account, "oldest", received_at=base)
    original = [selected] + [
        incoming(inbox_account, f"msg-{index}", received_at=base + timedelta(seconds=index)) for index in range(1, 106)
    ]
    _, first = read_page(owner_client, selected)
    assert len(first.events) == 50
    before = first.pages[0]["data-older-url"]
    assert "history_before=" in before and "history_page=" not in before
    newly_arrived = incoming(inbox_account, "arrived-after-page", received_at=base + timedelta(days=1))
    received = {event["data-event-id"] for event in first.events}
    while before:
        response, page = read_page(owner_client, selected, before)
        ids = {event["data-event-id"] for event in page.events}
        assert not (ids & received)
        assert len(ids) <= 50
        assert page.pages[0]["data-timeline-page-key"] == parse_qs(urlsplit(before).query)["history_before"][0]
        assert "inbox-reply-body" not in response.content.decode()
        received |= ids
        before = page.pages[0]["data-older-url"]
    assert received == {f"incoming:{message.pk}" for message in original}
    assert f"incoming:{newly_arrived.pk}" not in received


def test_keyset_ties_cover_all_event_kinds_without_duplicates(inbox_account, user):
    stamp = timezone.now() - timedelta(hours=1)
    selected = incoming(inbox_account, "anchor", received_at=stamp)
    for index in range(3):
        incoming(inbox_account, f"same-time-{index}", received_at=stamp)
        InboxReply.objects.create(inbox_message=selected, body="Sent", status="sent", sent_at=stamp)
        note = InternalNote.objects.create(inbox_message=selected, author=user, body="Note")
        InternalNote.objects.filter(pk=note.pk).update(created_at=stamp)
    messages = presentation.stored_thread_messages(selected)
    expected = {(kind, item.pk) for kind, item, _ in presentation.timeline_page(messages, 1)}
    found, boundary = set(), None
    while True:
        page = presentation.timeline_page(messages, 1, per_page=2, before=boundary)
        ids = {(kind, item.pk) for kind, item, _ in page}
        assert not (ids & found)
        found |= ids
        if not page.has_next():
            break
        boundary = presentation.parse_history_cursor(
            selected, presentation.history_cursor(selected, page.object_list[0])
        )
    assert found == expected


@pytest.mark.parametrize("failure", ["tampered", "other-anchor", "changed-thread", "expired", "empty"])
def test_invalid_cursor_is_content_free_and_does_not_mark_unread(owner_client, inbox_account, failure):
    message = incoming(inbox_account, "selected", status="unread")
    page = presentation.timeline_page(presentation.stored_thread_messages(message), 1)
    if failure == "expired":
        with patch("django.core.signing.time.time", return_value=1000):
            token = presentation.history_cursor(message, page.object_list[0])
    else:
        token = presentation.history_cursor(message, page.object_list[0])
    if failure == "tampered":
        token += "tampered"
    elif failure == "other-anchor":
        message = incoming(inbox_account, "another-anchor", status="unread")
    elif failure == "changed-thread":
        message.extra["conversation_id"] = "different-thread"
        message.save(update_fields=["extra"])
    elif failure == "empty":
        token = ""
    with patch("apps.inbox.native_thread_reads.read_native_thread") as provider:
        response = owner_client.get(detail_url(message), {"history_before": token})
    assert response.status_code == 400
    assert message.body.encode() not in response.content
    message.refresh_from_db()
    assert message.status == InboxMessage.Status.UNREAD
    provider.assert_not_called()


def test_cursor_cannot_change_account_scope(inbox_account):
    message = incoming(inbox_account, "selected")
    page = presentation.timeline_page(presentation.stored_thread_messages(message), 1)
    token = presentation.history_cursor(message, page.object_list[0])
    data = signing.loads(token, salt=presentation.HISTORY_CURSOR_SALT)
    assert set(data) == {"anchor", "workspace", "account", "thread", "before"}
    assert message.body not in str(data)
    message.social_account_id = message.workspace_id  # Scope changes invalidate even correctly signed positions.
    with pytest.raises(ValueError):
        presentation.parse_history_cursor(message, token)


def test_transient_view_scope_preserves_status_changes_but_not_identity_changes(inbox_account, user):
    message = incoming(inbox_account, "anchor")
    marker = presentation.native_view_scope(message, user.pk)
    assert len(marker) == 64  # HMAC equality marker, not raw identity or a read credential.
    message.status = "resolved"
    assert presentation.native_view_scope(message, user.pk) == marker
    InboxReply.objects.create(inbox_message=message, body="Saved draft", status="draft")
    assert presentation.native_view_scope(message, user.pk) == marker
    assert presentation.native_view_scope(message, message.pk) != marker
    message.extra["conversation_id"] = "other-thread"
    assert presentation.native_view_scope(message, user.pk) != marker
    message.extra["conversation_id"] = "thread-a"
    message.social_account.oauth_access_token = "synthetic-rotated-credential"
    assert presentation.native_view_scope(message, user.pk) != marker
    assert "synthetic" not in presentation.native_view_scope(message, user.pk)


@pytest.mark.parametrize("unavailable", ["actor", "thread", "account", "expiry", "group"])
def test_transient_view_scope_never_preserves_known_unavailable_context(inbox_account, user, unavailable):
    message = incoming(inbox_account, "anchor")
    actor = user.pk
    if unavailable == "actor":
        actor = None
    elif unavailable == "thread":
        message.extra.pop("conversation_id")
    elif unavailable == "account":
        message.social_account.connection_status = "disconnected"
    elif unavailable == "expiry":
        message.social_account.token_expires_at = timezone.now() - timedelta(seconds=1)
    else:
        message.extra["participant_ids"].append("third-person")
    assert presentation.native_view_scope(message, actor) == ""


def test_transient_view_scope_rechecks_canonical_peer_revision_and_sibling_conflicts(inbox_account, user):
    message = incoming(inbox_account, "anchor")
    conversation = InboxConversation.objects.create(
        workspace=message.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        platform_conversation_id="thread-a",
        peer_id="peer-a",
        identity_kind="platform",
        conversation_type="direct",
        classification_reason="participants_pair",
    )
    ConversationMessage.objects.create(
        workspace=message.workspace,
        social_account=inbox_account,
        platform=inbox_account.platform,
        conversation=conversation,
        platform_message_id=message.platform_message_id,
        legacy_message=message,
        direction="inbound",
        sender_id="peer-a",
        recipient_id=inbox_account.account_platform_id,
        conversation_type="direct",
        classification_reason="participants_pair",
        occurred_at=message.received_at,
    )
    message = InboxMessage.objects.select_related("social_account").get(pk=message.pk)
    marker = presentation.native_view_scope(message, user.pk)
    assert marker
    InboxConversation.objects.filter(pk=conversation.pk).update(revision=1)
    assert presentation.native_view_scope(message, user.pk) not in ("", marker)
    InboxConversation.objects.filter(pk=conversation.pk).update(peer_id="another-peer")
    assert presentation.native_view_scope(message, user.pk) == ""
    InboxConversation.objects.filter(pk=conversation.pk).update(peer_id="peer-a", peer_ambiguous=True)
    assert presentation.native_view_scope(message, user.pk) == ""
    InboxConversation.objects.filter(pk=conversation.pk).update(peer_ambiguous=False)
    incoming(
        inbox_account,
        "conflicting-sibling",
        extra={
            "conversation_id": "thread-a",
            "conversation_type": "group",
            "classification_reason": "participants_group",
        },
    )
    assert presentation.native_view_scope(message, user.pk) == ""
