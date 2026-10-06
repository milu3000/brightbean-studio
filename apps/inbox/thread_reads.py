"""Bounded, read-only thread projection shared by REST and MCP.

Only existing inbox records are read. No provider calls, ledger capture,
work-status changes, internal notes, or new authorization are involved.
"""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from django.core import signing
from django.db.models import Q
from django.utils import timezone

from .presentation import stored_thread_messages, thread_id

_SALT = "brightbean.stored-inbox-thread.v1"
_PAGE_BUDGET = 65536
_ITEM_BUDGET = 24000


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def _bounded_message(message):
    from apps.api.schemas import InboxMessageResponse, InboxReplyResponse

    value = InboxMessageResponse.from_message(message, include_eligibility=True).model_dump(mode="json")
    value["body_truncated"] = len(value["body"]) > 2000
    value["body"] = value["body"][:2000]
    value["content_preview"] = value["content_preview"][:2000]
    value["attachment_metadata_count"] = len(value["attachments"])
    value["attachments"] = value["attachments"][:3]
    for attachment in value["attachments"]:
        attachment["title"] = attachment["title"][:500]
        for key in ("url", "preview_url"):
            if _size(attachment[key]) > 4096:
                attachment[key] = ""
                attachment["availability"] = "unavailable"
                attachment["availability_reason"] = "size_limited"
    value["reply_count"] = message.replies.count()
    for reply in message.replies.select_related("author").order_by("-created_at", "-pk")[:3]:
        item = InboxReplyResponse.from_reply(reply).model_dump(mode="json")
        item["body"] = item["body"][:2000]
        item["send_error"] = item["send_error"][:500]
        value["replies"].append(item)
    value["detail_tool"] = "get_inbox_message"
    while _size(value) > _ITEM_BUDGET and value["attachments"]:
        value["attachments"].pop()
    while _size(value) > _ITEM_BUDGET and value["replies"]:
        value["replies"].pop()
    if _size(value) > _ITEM_BUDGET:
        value["body_truncated"] = value["body_truncated"] or len(value["body"]) > 500
        value["body"] = value["body"][:500]
        value["content_preview"] = value["content_preview"][:500]
    value["attachments_truncated"] = len(value["attachments"]) < value["attachment_metadata_count"]
    value["replies_truncated"] = len(value["replies"]) < value["reply_count"] or any(
        len(reply.body) > 2000 for reply in message.replies.order_by("-created_at", "-pk")[: len(value["replies"])]
    )
    return value


def read_stored_thread(message, *, actor_id, cursor=None, limit=20):
    """Caller must resolve the anchor through its current permission/allowlist."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        raise ValueError("cursor must be a bounded string")
    native_id = thread_id(message)
    scope = {
        "v": 1,
        "actor": str(actor_id),
        "workspace": str(message.workspace_id),
        "account": str(message.social_account_id),
        "anchor": str(message.pk),
        "thread": hashlib.sha256(native_id.encode()).hexdigest(),
    }
    qs = stored_thread_messages(message).select_related("social_account").order_by("-received_at", "-pk")
    latest = qs.values_list("pk", flat=True).first()
    if cursor:
        try:
            data = signing.loads(cursor, salt=_SALT, max_age=3600)
            if not isinstance(data, dict) or data.get("scope") != scope:
                raise ValueError
            before = datetime.fromisoformat(data["before"])
            before_id = UUID(data["before_id"])
            if timezone.is_naive(before):
                raise ValueError
        except (signing.BadSignature, ValueError, TypeError, KeyError) as exc:
            raise ValueError("cursor does not match this thread and current caller") from exc
        qs = qs.filter(Q(received_at__lt=before) | Q(received_at=before, pk__lt=before_id))
    rows = list(qs[: limit + 1])
    result = {
        "anchor_message_id": str(message.pk),
        "latest_message_id": str(latest) if latest else None,
        "grouping": "native_conversation" if native_id else "single_message",
        "messages": [],
        "limit": limit,
        "next_cursor": None,
        "history_complete": False,
        "outbound_coverage": "brightbean_replies_only",
        "response_truncated": False,
    }
    consumed = []
    for row in rows[:limit]:
        item = _bounded_message(row)
        if _size(result) + _size(item) + 2048 > _PAGE_BUDGET:
            result["response_truncated"] = True
            break
        result["messages"].append(item)
        consumed.append(row)
    if consumed and len(rows) > len(consumed):
        last = consumed[-1]
        result["next_cursor"] = signing.dumps(
            {"scope": scope, "before": last.received_at.isoformat(), "before_id": str(last.pk)}, salt=_SALT
        )
    return result
