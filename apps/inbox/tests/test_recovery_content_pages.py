"""Every shortened recovery preview has an authorized bounded continuation."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.inbox import canonical_reads as reader
from apps.inbox import preserved_details
from apps.inbox.models import ConversationMessage, ConversationObservationState, InboxReply, InternalNote
from apps.inbox.tests.test_basic_recovery import browser as _browser
from apps.inbox.tests.test_basic_recovery import get, link, original, url
from apps.inbox.tests.test_canonical_reads_rebuilt import context as _context
from apps.inbox.tests.test_canonical_reads_rebuilt import proof, row

context = _context
browser = _browser
pytestmark = pytest.mark.django_db


def test_canonical_full_body_service_and_real_html_continue_to_final_text(context, browser):
    body = "A" * 4000 + "MIDDLE" + "尾" * 2500 + " FINAL TEXT"
    message = row(context, body=body)
    text, cursor = "", None
    while True:
        result = reader.read_message_body(context.scope, message.pk, cursor=cursor)
        assert len(result["body"]) <= 2000
        text += result["body"]
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert text == body
    detail = get(browser, url(context, "basic_detail", conversation_id=context.conversation.pk))
    response = get(browser, link(detail, "Read full text"))
    for _index in range(10):
        if not response.context["next_url"]:
            break
        response = get(browser, link(response, "Continue reading"))
    assert "FINAL TEXT" in response.content.decode()
    assert "<script" not in response.content.decode()


def test_canonical_attachment_ui_pages_every_item_and_holds_wrong_path(context, browser):
    message = row(
        context,
        attachments=[
            {"type": "share", "url": f"https://example.com/{index}", "title": f"MEDIA {index}"} for index in range(8)
        ],
    )
    detail = get(browser, url(context, "basic_detail", conversation_id=context.conversation.pk))
    response = get(browser, link(detail, "All attachments"))
    bodies = []
    for _index in range(6):
        bodies.append(response.content.decode())
        if not response.context["next_url"]:
            break
        response = get(browser, link(response, "Continue reading"))
    assert all(f"MEDIA {index}" in "".join(bodies) for index in range(8))
    wrong = url(context, "basic_message_content", conversation_id=uuid4(), message_id=message.pk)
    assert get(browser, wrong).status_code == 404


@pytest.mark.parametrize("change", ["withdrawn", "expired", "body", "scope", "message"])
def test_canonical_body_continuation_does_not_survive_policy_or_identity_change(context, change):
    message = row(context, body="PRIVATE BODY" * 1000)
    proof(context, message)
    first = reader.read_message_body(context.scope, message.pk)
    if change == "withdrawn":
        ConversationObservationState.objects.filter(message=message).update(withdrawn_at=timezone.now())
    elif change == "expired":
        ConversationObservationState.objects.filter(message=message).update(expired_at=timezone.now())
    elif change == "body":
        ConversationMessage.objects.filter(pk=message.pk).update(body="REPLACEMENT BODY")
    elif change == "scope":
        context.member.delete()
    else:
        ConversationMessage.objects.filter(pk=message.pk).update(platform_message_id="replacement-id")
    with pytest.raises(reader.CanonicalReadError):
        reader.read_message_body(context.scope, message.pk, cursor=first["next_cursor"])


def test_planned_deadline_does_not_stop_human_body_continuation(context):
    message = row(context, body="VISIBLE" * 1000)
    proof(context, message, expires_at=timezone.now() - timedelta(days=500))
    assert reader.read_message_body(context.scope, message.pk)["body"]


def test_preserved_full_body_and_media_are_readable_after_preview(context, browser):
    body = "P" * 5500 + " PRESERVED END"
    record = original(
        context,
        body=body,
        extra={
            "inbox_attachments": [
                {"type": "share", "url": f"https://example.com/p{index}", "title": f"ATTACHMENT {index}"}
                for index in range(9)
            ]
        },
    )
    detail = get(browser, url(context, "basic_preserved_detail", message_id=record.pk))
    response = get(browser, link(detail, "Read full text"))
    fragments = []
    for _index in range(10):
        fragments.append(response.context["content"]["body"])
        if not response.context["next_url"]:
            break
        response = get(browser, link(response, "Continue reading"))
    assert "".join(fragments) == body
    response = get(browser, link(detail, "All attachments"))
    items = []
    for _index in range(10):
        items.extend(response.context["content"]["attachments"])
        if not response.context["next_url"]:
            break
        response = get(browser, link(response, "Continue reading"))
    assert len(items) == 9 and items[-1]["title"] == "ATTACHMENT 8"


def test_preserved_replies_drafts_and_notes_have_real_identity_and_body_continuation(context, browser):
    record = original(context)
    for index in range(8):
        InboxReply.objects.create(
            inbox_message=record,
            author=context.user,
            body=f"REPLY {index} " + "R" * 4300 + " REPLY END",
            status="draft" if index % 2 else "sent",
        )
        InternalNote.objects.create(
            inbox_message=record, author=context.user, body=f"NOTE {index} " + "N" * 4100 + " NOTE END"
        )
    reply_count = InboxReply.objects.count()
    detail = get(browser, url(context, "basic_preserved_detail", message_id=record.pk))
    assert "BrightBean replies and drafts" in detail.content.decode() and "Internal notes" in detail.content.decode()
    assert "REPLY 7" in detail.content.decode() and "NOTE 7" in detail.content.decode()
    seen = []
    cursor = None
    while True:
        page = preserved_details.list_related(context.scope, record.pk, kind="replies", cursor=cursor)
        seen.extend(item["id"] for item in page["records"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(set(seen)) == 8
    reply = InboxReply.objects.filter(inbox_message=record).first()
    note = InternalNote.objects.filter(inbox_message=record).first()
    for part, item in [("replies", reply), ("notes", note)]:
        fragments, cursor = [], None
        while True:
            value = preserved_details.read_part(context.scope, record.pk, part=part, item_id=item.pk, cursor=cursor)
            fragments.append(value["body"])
            cursor = value["next_cursor"]
            if cursor is None:
                break
        assert "".join(fragments) == item.body
    assert InboxReply.objects.count() == reply_count and not ConversationMessage.objects.exists()


def test_preserved_reply_restriction_never_falls_back_to_raw_saved_text(context, browser):
    record = original(context)
    reply = InboxReply.objects.create(
        inbox_message=record, body="PRIVATE COMPACTED", status="sent", content_compacted_at=timezone.now()
    )
    response = get(browser, url(context, "basic_preserved_detail", message_id=record.pk))
    assert "PRIVATE COMPACTED" not in response.content.decode() and "Content expired" in response.content.decode()
    value = preserved_details.read_part(context.scope, record.pk, part="replies", item_id=reply.pk)
    assert value["body"] == "" and value["is_expired"]


@pytest.mark.parametrize("part", ["body", "attachments", "replies", "notes"])
def test_preserved_detail_cursor_current_grants_and_original_fk_are_required(context, part):
    record = original(
        context,
        body="SAVED" * 1000,
        extra={"inbox_attachments": [{"type": "share", "url": f"https://example.com/{i}"} for i in range(5)]},
    )
    other = original(context, platform_message_id="other-original")
    item = None
    if part == "replies":
        item = InboxReply.objects.create(inbox_message=record, body="REPLY" * 1000)
    elif part == "notes":
        item = InternalNote.objects.create(inbox_message=record, body="NOTE" * 1000)
    args = {"part": part, "item_id": item.pk if item else None, "limit": 1 if part == "attachments" else 2000}
    first = preserved_details.read_part(context.scope, record.pk, **args)
    with pytest.raises(reader.CanonicalReadError):
        preserved_details.read_part(context.scope, other.pk, cursor=first["next_cursor"], **args)
    context.member.delete()
    with pytest.raises(reader.CanonicalReadError):
        preserved_details.read_part(context.scope, record.pk, cursor=first["next_cursor"], **args)


def test_preserved_body_cursor_detects_canonical_shadow_added_after_page(context):
    record = original(context, body="RAW PRIVATE" * 1000, platform_message_id="previously-unlinked")
    first = preserved_details.read_part(context.scope, record.pk)
    row(
        context,
        platform_message_id=record.platform_message_id,
        direction="inbound",
        body="WITHDRAWN PRIVATE",
        is_deleted=True,
    )
    with pytest.raises(reader.CanonicalReadError):
        preserved_details.read_part(context.scope, record.pk, cursor=first["next_cursor"])
    assert preserved_details.read_part(context.scope, record.pk)["body"] == ""


def test_message_body_and_media_cursors_survive_unrelated_conversation_arrivals(context):
    message = row(
        context,
        body="ORIGINAL" * 1200,
        attachments=[{"type": "share", "url": f"https://example.com/media-{index}"} for index in range(7)],
    )
    body = reader.read_message_body(context.scope, message.pk)
    media = reader.read_message_attachments(context.scope, message.pk, limit=3)
    row(context, direction="inbound", body="UNRELATED NEW MESSAGE")
    continuation = reader.read_message_body(context.scope, message.pk, cursor=body["next_cursor"])
    assert continuation["body"] and continuation["body_offset"] == 2000
    continued_media = reader.read_message_attachments(context.scope, message.pk, cursor=media["next_cursor"], limit=3)
    assert len(continued_media["items"]) == 3
    assert continued_media["items"][0]["url"] == "https://example.com/media-3"
