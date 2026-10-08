"""Human-only continuation of original content, saved replies/drafts and notes."""

from datetime import datetime

from django.core import signing
from django.db.models import Q

from . import preserved_history as history
from .canonical_access import digest
from .canonical_reads import CanonicalReadError, _uuid
from .models import InboxReply, InternalNote
from .receipt_compaction import reply_display_content

SALT = "brightbean.preserved-details.v1"


def _record(scope, message_id):
    accounts, stamp = history._scope(scope)
    record = history._records(scope, accounts).filter(pk=_uuid(message_id)).first()
    if record is None:
        raise history._denied()
    return accounts, stamp, record


def _related(record, kind):
    if kind == "replies":
        return InboxReply.objects.filter(inbox_message=record).select_related("author")
    if kind == "notes":
        return InternalNote.objects.filter(inbox_message=record).select_related("author")
    raise CanonicalReadError("invalid_filter", "Unknown saved record type.")


def _item(row, kind):
    content = (
        reply_display_content(row)
        if kind == "replies"
        else {"body": row.body, "available": True, "is_deleted": False, "is_expired": False, "content_status": "text"}
    )
    return {
        "id": str(row.pk),
        "kind": kind,
        "author": row.author.name if row.author_id else "Former member",
        "created_at": row.created_at.isoformat(),
        "status": row.status if kind == "replies" else "Internal note",
        **content,
    }


def list_related(scope, message_id, *, kind, cursor=None, limit=5):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise CanonicalReadError("invalid_limit", "Invalid saved-record page size.")
    accounts, stamp, record = _record(scope, message_id)
    qs = _related(record, kind)
    binding = [
        stamp,
        str(record.pk),
        str(record.workspace_id),
        str(record.social_account_id),
        record.platform_message_id,
        kind,
        limit,
    ]
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError
            value = signing.loads(cursor, salt=SALT + ".list", max_age=3600)
            if value["scope"] != binding:
                raise ValueError
            timestamp, pk = datetime.fromisoformat(value["time"]), _uuid(value["id"])
            qs = qs.filter(Q(created_at__lt=timestamp) | Q(created_at=timestamp, pk__lt=pk))
        except (ValueError, TypeError, KeyError, signing.BadSignature) as exc:
            raise CanonicalReadError("stale_cursor", "The saved-record page changed or expired.") from exc
    rows = list(qs.order_by("-created_at", "-pk")[: limit + 1])
    items = []
    for item in rows[:limit]:
        value = _item(item, kind)
        value["body_truncated"] = len(value["body"]) > 2000
        value["body"] = value["body"][:2000]
        # A draft/send-error is not needed to read the saved text here.
        value.pop("send_error", None)
        items.append(value)
    history._recheck(scope, stamp)
    current = (
        history._records(scope, accounts)
        .filter(
            pk=record.pk, social_account_id=record.social_account_id, platform_message_id=record.platform_message_id
        )
        .first()
    )
    if current is None:
        raise history._denied()
    # Re-evaluate each policy immediately before releasing this bounded page.
    for old, value in zip(rows, items, strict=False):
        fresh = _related(current, kind).filter(pk=old.pk).first()
        if fresh is None or _item(fresh, kind)["body"][:2000] != value["body"]:
            raise CanonicalReadError("stale_revision", "The saved record changed while reading.")
    history._recheck(scope, stamp)
    more = len(rows) > limit
    return {
        "kind": kind,
        "records": items,
        "next_cursor": signing.dumps(
            {"scope": binding, "time": rows[limit - 1].created_at.isoformat(), "id": str(rows[limit - 1].pk)},
            salt=SALT + ".list",
        )
        if more
        else None,
    }


def read_part(scope, message_id, *, part="body", item_id=None, cursor=None, limit=2000):
    if part not in {"body", "attachments", "replies", "notes"}:
        raise CanonicalReadError("invalid_filter", "Unknown saved-content section.")
    maximum = 10 if part == "attachments" else 4000
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise CanonicalReadError("invalid_limit", "Invalid saved-content page size.")
    accounts, stamp, record = _record(scope, message_id)

    def current_content():
        original = (
            history._records(scope, accounts)
            .filter(
                pk=record.pk, social_account_id=record.social_account_id, platform_message_id=record.platform_message_id
            )
            .first()
        )
        if original is None:
            raise history._denied()
        if part in {"replies", "notes"}:
            item = _related(original, part).filter(pk=_uuid(item_id)).first()
            if item is None:
                raise history._denied()
            return _item(item, part)
        return history._content(original, accounts[original.social_account_id])[1]

    content = current_content()
    selected = content.get("attachments", []) if part == "attachments" else content["body"]
    binding = [
        stamp,
        str(record.pk),
        str(record.workspace_id),
        str(record.social_account_id),
        record.platform_message_id,
        part,
        str(item_id),
        digest(content),
        limit,
    ]
    offset = 0
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError
            value = signing.loads(cursor, salt=SALT + ".part", max_age=3600)
            offset = value["offset"]
            if (
                value["scope"] != binding
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or not 0 <= offset <= len(selected)
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, signing.BadSignature) as exc:
            raise CanonicalReadError("stale_cursor", "The saved content changed or its page expired.") from exc
    portion = selected[offset : offset + limit]
    if part == "attachments":
        from .canonical_reads import _size

        projected = []
        for item in portion:
            value = {
                key: item.get(key, "")
                for key in ("type", "title", "url", "preview_url", "availability", "availability_reason")
            }
            value["title"] = str(value["title"])[:500]
            for key in ("url", "preview_url"):
                if _size(value) > 20000:
                    value[key] = ""
                    value["availability"] = "unavailable"
                    value["availability_reason"] = "size_limited"
            if projected and _size(projected + [value]) > 30000:
                break
            projected.append(value)
        portion = projected
    end = offset + len(portion)
    history._recheck(scope, stamp)
    if digest(current_content()) != digest(content):
        raise CanonicalReadError("stale_revision", "The saved content changed while reading.")
    history._recheck(scope, stamp)
    return {
        "source": "preserved_legacy_record",
        "id": str(record.pk),
        "part": part,
        "item_id": str(item_id) if item_id else None,
        "body": portion if part != "attachments" else "",
        "attachments": portion if part == "attachments" else [],
        "available": content["available"],
        "is_deleted": content["is_deleted"],
        "is_expired": content["is_expired"],
        "content_status": content["content_status"],
        "offset": offset,
        "has_more": end < len(selected),
        "next_cursor": signing.dumps({"scope": binding, "offset": end}, salt=SALT + ".part")
        if end < len(selected)
        else None,
    }
