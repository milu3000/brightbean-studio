"""Human-only, read-only access to preserved original legacy DM records.

This is a projection of existing InboxMessage rows, not another message store
or a claim of native conversation identity. Canonical shadow privacy always
takes precedence over a raw legacy body, even without a legacy FK.
"""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.db.models import Q
from django.utils import timezone

from providers.meta_inbox_content import is_deleted_content, message_content_status, normalize_attachments

from .canonical_content import visible_content
from .canonical_reads import CanonicalReadError, CanonicalReadScope, enabled
from .models import ConversationMessage, InboxMessage

SALT = "brightbean.preserved-history.read.v1"
SCAN_LIMIT = 50


def _denied():
    return CanonicalReadError("not_found_or_denied", "Preserved history is unavailable in this scope.")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _uuid(value):
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _denied() from exc


def _scope(scope):
    if (
        not isinstance(scope, CanonicalReadScope)
        or not scope.principal.startswith("session:")
        or not enabled()
        or getattr(settings, "INBOX_CONVERSATION_V2_ENABLED", False) is not True
    ):
        raise _denied()
    queryset, grants = scope.refresh()
    accounts = {
        account.pk: account
        for account in queryset.filter(workspace_id=scope.workspace_id)
        .only("pk", "workspace_id", "platform", "account_platform_id", "webhook_target_id", "account_name")
        .order_by("pk")
    }
    token = _digest(
        [
            scope.principal,
            scope.workspace_id,
            grants,
            [
                [item.pk, item.workspace_id, item.platform, item.account_platform_id, item.webhook_target_id]
                for item in accounts.values()
            ],
        ]
    )
    return accounts, token


def _recheck(scope, expected):
    if _scope(scope)[1] != expected:
        raise CanonicalReadError("stale_scope", "The current preserved-history permissions changed.")


def _records(scope, accounts):
    return InboxMessage.objects.filter(
        workspace_id=scope.workspace_id,
        social_account_id__in=accounts,
        social_account__workspace_id=scope.workspace_id,
        message_type="dm",
    ).exclude(pk__in=InboxMessage.objects.filter(extra__transport_projection=True).values("pk"))


def _shadow(record, account):
    identity = Q(legacy_message_id=record.pk)
    # A platform rebind is a privacy hold, not grounds to forget an older
    # canonical shadow. Compare native IDs only within the exact account UUID.
    if record.platform_message_id:
        identity |= Q(social_account_id=account.pk, platform_message_id=record.platform_message_id)
    rows = list(
        ConversationMessage.objects.filter(identity)
        .only(
            "pk",
            "workspace_id",
            "social_account_id",
            "platform",
            "platform_message_id",
            "conversation_id",
            "legacy_message_id",
            "legacy_reply_id",
            "direction",
        )
        .order_by("pk")[:2]
    )
    if not rows:
        return None, None
    row = rows[0]
    if (
        len(rows) != 1
        or row.direction != "inbound"
        or row.platform_message_id != record.platform_message_id
        or (row.workspace_id, row.social_account_id, row.platform)
        != (record.workspace_id, record.social_account_id, account.platform)
    ):
        return None, {
            "available": False,
            "body": "",
            "attachments": [],
            "is_deleted": False,
            "is_expired": False,
            "content_status": "unavailable",
        }
    return row, visible_content(row)


def _content(record, account):
    extra = record.extra if isinstance(record.extra, dict) else {}
    tombstone = is_deleted_content(extra)
    declared_expired = extra.get("inbox_content_status") == "expired" or extra.get("content_status") == "expired"
    canonical, content = _shadow(record, account)
    if content is None:
        body = "" if tombstone or declared_expired else record.body or ""
        attachments = [] if tombstone or declared_expired else normalize_attachments(extra)
        content = {
            "available": not tombstone and not declared_expired,
            "body": body,
            "attachments": attachments,
            "is_deleted": tombstone,
            "is_expired": declared_expired,
            "content_status": "expired"
            if declared_expired
            else "removed"
            if tombstone
            else message_content_status(extra, body),
        }
    elif tombstone or declared_expired:
        content = {
            **content,
            "available": False,
            "body": "",
            "attachments": [],
            "is_deleted": content["is_deleted"] or tombstone,
            "is_expired": content["is_expired"] or declared_expired,
            "content_status": "expired" if declared_expired or content["is_expired"] else "removed",
        }
    return canonical, content


def _project(record, account):
    canonical, content = _content(record, account)
    attachments = []
    for item in content["attachments"][:3]:
        value = {key: item.get(key, "") for key in ("type", "url", "title", "availability")}
        value["title"] = str(value["title"])[:500]
        if len(str(value["url"])) > 2000:
            value["url"], value["availability"] = "", "unavailable"
        attachments.append(value)
    return {
        "id": str(record.pk),
        "id_namespace": "inbox_message",
        "source": "preserved_legacy_record",
        "workspace_id": str(record.workspace_id),
        "social_account_id": str(record.social_account_id),
        "account_name": account.account_name,
        "platform": account.platform,
        "sender_name": record.sender_name,
        "sender_handle": record.sender_handle,
        "saved_at": record.created_at.isoformat(),
        "recorded_received_at": record.received_at.isoformat(),
        "body": content["body"][:4000],
        "body_truncated": len(content["body"]) > 4000,
        "attachments": attachments,
        "attachments_truncated": len(content["attachments"]) > len(attachments),
        "is_deleted": content["is_deleted"],
        "is_expired": content["is_expired"],
        "content_available": content["available"],
        "content_status": content["content_status"],
        "linked": canonical is not None,
        "canonical_direction": canonical.direction if canonical else None,
        "canonical_message_id": str(canonical.pk) if canonical else None,
        "canonical_conversation_id": str(canonical.conversation_id)
        if canonical and canonical.conversation_id
        else None,
        "send_authorized": False,
    }


def _unchanged(scope, accounts, pairs):
    for record, expected in pairs:
        current = (
            _records(scope, accounts)
            .filter(
                pk=record.pk, social_account_id=record.social_account_id, platform_message_id=record.platform_message_id
            )
            .first()
        )
        if current is None or _project(current, accounts[current.social_account_id]) != expected:
            raise CanonicalReadError("stale_revision", "The preserved record changed while reading.")


def available_accounts(scope):
    accounts, token = _scope(scope)
    ids = set(_records(scope, accounts).values_list("social_account_id", flat=True).distinct())
    result = [
        {"id": str(item.pk), "account_name": item.account_name, "platform": item.platform}
        for item in accounts.values()
        if item.pk in ids
    ]
    _recheck(scope, token)
    return result


def read_record(scope, message_id):
    accounts, token = _scope(scope)
    record = _records(scope, accounts).filter(pk=_uuid(message_id)).first()
    if record is None:
        raise _denied()
    result = _project(record, accounts[record.social_account_id])
    _unchanged(scope, accounts, [(record, result)])
    _recheck(scope, token)
    return result


def list_records(scope, *, social_account_id=None, search="", cursor=None, limit=20):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
        raise CanonicalReadError("invalid_limit", "Invalid preserved-history page size.")
    if not isinstance(search, str) or len(search) > 500:
        raise CanonicalReadError("invalid_search", "Invalid preserved-history search.")
    accounts, token = _scope(scope)
    records = _records(scope, accounts)
    if social_account_id:
        account_id = _uuid(social_account_id)
        if account_id not in accounts:
            raise _denied()
        records = records.filter(social_account_id=account_id)
    search = search.strip().casefold()
    binding = [token, str(social_account_id), _digest(search), limit]
    snapshot, position = timezone.now(), None
    if cursor:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                raise ValueError
            value = signing.loads(cursor, salt=SALT, max_age=3600)
            if value["scope"] != binding:
                raise ValueError
            snapshot = datetime.fromisoformat(value["snapshot"])
            stamp = datetime.fromisoformat(value["position"][0])
            if timezone.is_naive(snapshot) or timezone.is_naive(stamp):
                raise ValueError
            position = stamp, UUID(value["position"][1])
        except (ValueError, TypeError, KeyError, signing.BadSignature) as exc:
            raise CanonicalReadError("stale_cursor", "The preserved-history page changed or expired.") from exc
    records = records.filter(created_at__lte=snapshot)
    if position:
        records = records.filter(Q(created_at__lt=position[0]) | Q(created_at=position[0], pk__lt=position[1]))
    rows = list(records.order_by("-created_at", "-pk")[: SCAN_LIMIT + 1])
    results, checked = [], []
    for record in rows[:SCAN_LIMIT]:
        projected = _project(record, accounts[record.social_account_id])
        checked.append((record, projected))
        # Search only visible content. Raw-body SQL filtering would leak
        # expired/withdrawn matches through result presence or cursor behavior.
        text = " ".join(projected[key] for key in ("body", "sender_name", "sender_handle", "account_name")).casefold()
        if not search or search in text:
            results.append(projected)
        if len(results) >= limit:
            break
    _unchanged(scope, accounts, checked)
    _recheck(scope, token)
    more = len(rows) > len(checked)
    last = checked[-1][0] if checked else None
    continuation = (
        signing.dumps(
            {
                "scope": binding,
                "snapshot": snapshot.isoformat(),
                "position": [last.created_at.isoformat(), str(last.pk)],
            },
            salt=SALT,
        )
        if more and last
        else None
    )
    return {
        "source": "preserved_legacy_records",
        "records": results,
        "next_cursor": continuation,
        "limit": limit,
        "history_complete": False,
        "ordering": "saved_at_desc",
    }
