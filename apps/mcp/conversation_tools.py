"""Bounded, read-only views of the optional conversation ledger.

These tools never import history, mark work as read/resolved, refresh a provider,
or authorize a send. Native outgoing observations are context, not new inbox
work. The original inbox tools and inbound event contract remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from django.core import signing
from django.db.models import F, Q
from django.utils import timezone

from apps.inbox.conversation_capabilities import conversation_capabilities
from apps.inbox.conversation_policy import enrollment_identity, read_allowed, read_available
from apps.inbox.models import ConversationMessage, ConversationSyncState, InboxConversation, InboxMessage
from apps.mcp.handlers import _parse_uuid, _require_perm, _wrap_text
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tools import Tool, register_tool
from providers.meta_inbox_content import message_content_status, normalize_attachments

_FLAG = "INBOX_CONVERSATION_V2_ENABLED"
_CURSOR_SALT = "brightbean.conversation.read.v1"
_CURSOR_MAX_AGE = 24 * 60 * 60
_MAX_RESPONSE_CHARS = 65536
_MAX_MESSAGE_CHARS = 16384
_LIMITATIONS = [
    "Only observed, authorized DM history is available; coverage is not complete.",
    "Native activity may arrive late. No observed outgoing does not prove nobody replied on the platform.",
    "Reading does not mark work read or resolved, and does not authorize sending.",
]


def _iso(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def _limit(args, default=50):
    value = args.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise JsonRpcError(INVALID_PARAMS, "limit must be an integer between 1 and 100")
    return value


def _scope(context):
    _require_perm(context, "use_inbox")
    if not read_available():
        raise JsonRpcError(INVALID_PARAMS, "Conversation history is not enabled")
    key = context["api_key"]
    # Re-check the current account workspace as well as the ledger snapshot.
    # OAuth's ApiKey-shaped actor intentionally exposes only .all() on its
    # account shim. Use the shared contract, then filter the returned QuerySet.
    allowed_accounts = key.social_accounts.all().filter(workspace_id=key.workspace_id)
    accounts = {}
    identities = Q(pk__in=[])
    for account in allowed_accounts.only("id", "workspace_id", "platform"):
        identity = enrollment_identity(account)
        if identity is not None and read_allowed(account):
            accounts[account.pk] = identity
            identities |= Q(pk=identity[1], workspace_id=identity[0], platform=identity[2])
    # Keep this lazy: each protected SQL statement must prove the account is
    # still allowlisted and still has the exact identity enrolled above. A
    # platform=F(current account platform) check alone follows reassignment.
    eligible_accounts = allowed_accounts.filter(identities).values("pk")
    return (
        key,
        accounts,
        {
            "workspace_id": key.workspace_id,
            "social_account_id__in": eligible_accounts,
            "social_account__workspace_id": key.workspace_id,
            "platform": F("social_account__platform"),
        },
    )


def _cursor_scope(key, accounts, kind, filters):
    return {
        "v": 2,
        "key": str(key.id),
        "workspace": str(key.workspace_id),
        # IDs alone do not capture enrollment: the same account can be moved
        # between workspaces or platforms and later enrolled under its new identity.
        "accounts": hashlib.sha256(json.dumps(sorted(accounts.values()), separators=(",", ":")).encode()).hexdigest(),
        "kind": kind,
        "filters": filters,
    }


def _datetime(value):
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timezone.is_naive(parsed):
        raise ValueError
    return parsed


def _read_cursor(raw, scope):
    if raw is None:
        return timezone.now(), None
    try:
        if not isinstance(raw, str) or not raw or len(raw) > 4096:
            raise ValueError
        payload = signing.loads(raw, salt=_CURSOR_SALT, max_age=_CURSOR_MAX_AGE)
        if not isinstance(payload, dict) or payload.get("scope") != scope:
            raise ValueError
        snapshot = _datetime(payload["snapshot"])
        position = payload["position"]
        if not isinstance(position, list) or len(position) != 2:
            raise ValueError
        stamp = _datetime(position[0])
        item_id = _parse_uuid(position[1], "cursor")
        return snapshot, (stamp, item_id)
    except (signing.BadSignature, ValueError, KeyError, TypeError, JsonRpcError) as exc:
        raise JsonRpcError(INVALID_PARAMS, "Invalid, expired, or out-of-scope conversation cursor") from exc


def _write_cursor(scope, snapshot, row, date_field):
    return signing.dumps(
        {"scope": scope, "snapshot": _iso(snapshot), "position": [_iso(getattr(row, date_field)), str(row.pk)]},
        salt=_CURSOR_SALT,
        compress=True,
    )


def _page_query(qs, snapshot, position, date_field, limit):
    qs = qs.filter(**{f"{date_field}__lte": snapshot})
    if position:
        stamp, item_id = position
        qs = qs.filter(Q(**{f"{date_field}__lt": stamp}) | Q(**{date_field: stamp, "id__lt": item_id}))
    return list(qs.order_by(f"-{date_field}", "-id")[: limit + 1])


def _sync(account, scope):
    state = ConversationSyncState.objects.filter(**scope, social_account=account, stream="dm").first()
    return {
        "status": state.status if state else "unknown",
        "coverage": state.coverage if state else "unknown",
        "history_complete": False,
        "last_attempt_at": _iso(state.last_attempt_at) if state else None,
        "last_success_at": _iso(state.last_success_at) if state else None,
        "last_error_code": state.last_error_code if state else "",
        "note": "Account DM-stream observation only; not proof this entire conversation is current.",
    }


def _conversation(row):
    return {
        "id": str(row.pk),
        "workspace_id": str(row.workspace_id),
        "social_account_id": str(row.social_account_id),
        "platform": row.platform,
        "channel": "dm",
        "capabilities": conversation_capabilities(row.platform),
        "platform_conversation_id": row.platform_conversation_id,
        "peer_id": row.peer_id or None,
        "peer_ambiguous": row.peer_ambiguous,
        "identity_kind": row.identity_kind,
        "conversation_type": row.conversation_type,
        "classification_reason": row.classification_reason,
        "revision": row.revision,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _safe_attachments(attachments, *, deleted=False, limit=3):
    result = []
    # Revalidate even persisted data and hide internal provider attachment IDs.
    for item in normalize_attachments({"inbox_attachments": attachments, "is_deleted": deleted})[:limit]:
        projected = {
            key: item.get(key, "")
            for key in ("type", "url", "title", "preview_url", "availability", "availability_reason")
        }
        if len(json.dumps(projected)) > _MAX_MESSAGE_CHARS:
            projected["metadata_truncated"] = True
            for field in ("preview_url", "url"):
                if len(json.dumps(projected)) > _MAX_MESSAGE_CHARS:
                    projected[field] = ""
            if not projected["url"]:
                projected["availability"] = "unavailable"
                projected["availability_reason"] = "size_limited"
            if len(json.dumps(projected)) > _MAX_MESSAGE_CHARS:
                projected["title"] = ""
        result.append(projected)
    return result


def _row_content_status(row):
    if row.is_deleted:
        return "removed"
    if row.content_status in {
        "partial",
        "unsupported",
        "fields_unavailable",
        "link_provided",
        "unavailable",
        "text",
        "no_metadata",
    }:
        return row.content_status
    return message_content_status({"inbox_attachments": row.attachments}, row.body or "")


def _message(row):
    body = "" if row.is_deleted else row.body or ""
    attachments = _safe_attachments(row.attachments, deleted=row.is_deleted, limit=None)
    result: dict[str, Any] = {
        "id": str(row.pk),
        "conversation_id": str(row.conversation_id) if row.conversation_id else None,
        "conversation_attribution": row.conversation_attribution,
        "conversation_type": row.conversation_type,
        "classification_reason": row.classification_reason,
        "workspace_id": str(row.workspace_id),
        "social_account_id": str(row.social_account_id),
        "platform": row.platform,
        "platform_message_id": row.platform_message_id,
        "direction": row.direction,
        "sender_id": row.sender_id or None,
        "recipient_id": row.recipient_id or None,
        "sender_name": row.sender_name,
        "body": body[:2000],
        "body_truncated": len(body) > 2000,
        "attachments": attachments[:3],
        "attachment_metadata_count": len(attachments),
        "attachments_tool": "get_conversation_attachments",
        "attachments_truncated": len(attachments) > 3,
        "content_available": bool(body or any(item.get("url") for item in attachments)) and not row.is_deleted,
        "is_deleted": row.is_deleted,
        "content_status": _row_content_status(row),
        "occurred_at": _iso(row.occurred_at),
        "first_seen_at": _iso(row.first_seen_at),
        "updated_at": _iso(row.updated_at),
        "sources": row.sources,
        "delivery_status": row.delivery_status,
        "legacy_message_id": str(row.legacy_message_id) if row.legacy_message_id else None,
        "legacy_reply_id": str(row.legacy_reply_id) if row.legacy_reply_id else None,
    }
    # A message may contain huge URLs or multibyte text. Bound serialized JSON,
    # not just the number of rows; make every omission explicit.
    while len(json.dumps(result)) > _MAX_MESSAGE_CHARS and result["attachments"]:
        result["attachments"].pop()
        result["attachments_truncated"] = True
    while len(json.dumps(result)) > _MAX_MESSAGE_CHARS and result["body"]:
        result["body"] = result["body"][: len(result["body"]) // 2]
        result["body_truncated"] = True
    return result


def _bounded_page(rows, limit, scope, snapshot, date_field, serialize, metadata):
    items = []
    consumed = []
    for row in rows[:limit]:
        candidate = serialize(row)
        # Leave space for signed cursor and metadata. Each item is independently
        # bounded, so a single oversized message cannot stall pagination.
        if items and len(json.dumps({**metadata, "items": [*items, candidate]})) > _MAX_RESPONSE_CHARS - 4096:
            break
        items.append(candidate)
        consumed.append(row)
    more = len(rows) > len(consumed)
    return {
        **metadata,
        "items": items,
        "limit": limit,
        "has_more": more,
        "next_cursor": _write_cursor(scope, snapshot, consumed[-1], date_field) if more and consumed else None,
        "snapshot_at": _iso(snapshot),
    }


def _list_conversations(args: dict, context: dict[str, Any]) -> dict:
    key, accounts, scoped = _scope(context)
    limit = _limit(args)
    qs = InboxConversation.objects.filter(**scoped)
    account_id = args.get("social_account_id")
    if account_id is not None:
        account_id = _parse_uuid(account_id, "social_account_id")
        if account_id not in accounts:
            raise JsonRpcError(INVALID_PARAMS, "Account not found")
        qs = qs.filter(social_account_id=account_id)
    scope = _cursor_scope(key, accounts, "conversations", {"account": str(account_id) if account_id else None})
    snapshot, position = _read_cursor(args.get("cursor"), scope)
    rows = _page_query(qs, snapshot, position, "created_at", limit)
    result = _bounded_page(
        rows,
        limit,
        scope,
        snapshot,
        "created_at",
        _conversation,
        {
            "ordering": "created_at_desc",
            "unassigned_message_count": ConversationMessage.objects.filter(
                **scoped, conversation__isnull=True, **({"social_account_id": account_id} if account_id else {})
            ).count(),
            "limitations": _LIMITATIONS,
        },
    )
    return _wrap_text(result)


def _get_conversation_messages(args: dict, context: dict[str, Any]) -> dict:
    key, accounts, scoped = _scope(context)
    conversation = None
    if args.get("conversation_id") is not None:
        if "social_account_id" in args or "unassigned_only" in args:
            raise JsonRpcError(INVALID_PARAMS, "Choose a conversation or an explicitly scoped unassigned query")
        conversation_id = _parse_uuid(args.get("conversation_id"), "conversation_id")
        try:
            conversation = (
                InboxConversation.objects.filter(**scoped).select_related("social_account").get(pk=conversation_id)
            )
        except InboxConversation.DoesNotExist as exc:
            raise JsonRpcError(INVALID_PARAMS, "Conversation not found") from exc
        account = conversation.social_account
        filters: dict[str, Any] = {"conversation": str(conversation.pk)}
    else:
        if args.get("unassigned_only") is not True:
            raise JsonRpcError(INVALID_PARAMS, "Unassigned history requires social_account_id and unassigned_only=true")
        account_id = _parse_uuid(args.get("social_account_id"), "social_account_id")
        if account_id not in accounts:
            raise JsonRpcError(INVALID_PARAMS, "Account not found")
        account = (
            key.social_accounts.all()
            .filter(pk=account_id, pk__in=scoped["social_account_id__in"])
            .only("id", "workspace_id", "platform")
            .first()
        )
        if account is None:
            raise JsonRpcError(INVALID_PARAMS, "Account not found")
        filters = {"account": str(account.pk), "unassigned_only": True}
    limit = _limit(args)
    scope = _cursor_scope(key, accounts, "messages", filters)
    snapshot, position = _read_cursor(args.get("cursor"), scope)
    qs = ConversationMessage.objects.filter(**scoped, social_account=account, conversation=conversation)
    rows = _page_query(qs, snapshot, position, "first_seen_at", limit)
    result = _bounded_page(
        rows,
        limit,
        scope,
        snapshot,
        "first_seen_at",
        _message,
        {
            "conversation": _conversation(conversation) if conversation else None,
            "social_account_id": str(account.pk),
            "unassigned_only": conversation is None,
            "ordering": "first_seen_at_desc",
            "sync": _sync(account, scoped),
            "limitations": _LIMITATIONS
            + [
                "Pagination freezes newly observed rows, not edits. Re-read after revision changes.",
                "Observation order is not necessarily provider send order; inspect occurred_at.",
            ],
        },
    )
    return _wrap_text(result)


def _get_reply_context(args: dict, context: dict[str, Any]) -> dict:
    _key, _accounts, scoped = _scope(context)
    target_id = _parse_uuid(args.get("message_id"), "message_id")
    limit = _limit(args, default=20)
    # The legacy inbox ID remains the bridge from existing inbox.dm.received
    # payloads; reading context never changes its archived/unread state.
    legacy_scope = {name: value for name, value in scoped.items() if name != "platform"}
    try:
        original = (
            InboxMessage.objects.filter(**legacy_scope, message_type="dm")
            .select_related("social_account")
            .get(pk=target_id)
        )
    except InboxMessage.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Inbox message not found") from exc
    target = ConversationMessage.objects.filter(
        **scoped, legacy_message=original, social_account_id=original.social_account_id
    ).first()
    conversation = None
    if target and target.conversation_id:
        # A historical/corrupt cross-account FK is not authorization to reveal
        # another thread, even when the caller may separately access both.
        conversation = InboxConversation.objects.filter(
            **scoped, pk=target.conversation_id, social_account_id=target.social_account_id
        ).first()
    projected_target = _message(target) if target else None
    if projected_target and conversation is None:
        projected_target["conversation_id"] = None
        projected_target["conversation_attribution"] = ""
    result: dict[str, Any] = {
        "message_id": str(original.pk),
        "social_account_id": str(original.social_account_id),
        "capabilities": conversation_capabilities(original.social_account.platform),
        "legacy_work_status": original.status,
        "context_status": "available" if conversation else "unassigned" if target else "not_imported",
        "target": projected_target,
        "conversation": _conversation(conversation) if conversation else None,
        "items": [],
        "has_more": False,
        "newer_outbound_observed": None,
        "sync": _sync(original.social_account, scoped),
        "send_preconditions_enforced": False,
        "limitations": _LIMITATIONS
        + [
            "This first phase supplies context only; V2 send-version checks are not yet implemented.",
            "An outgoing observation does not establish which inbound question it answered.",
        ],
    }
    if target and conversation:
        qs = ConversationMessage.objects.filter(
            **scoped, conversation=conversation, social_account_id=original.social_account_id
        )
        rows = list(qs.order_by("-first_seen_at", "-id")[: limit + 1])
        for row in rows[:limit]:
            item = _message(row)
            if len(json.dumps({**result, "items": [*result["items"], item]})) > _MAX_RESPONSE_CHARS - 1024:
                break
            result["items"].append(item)
        result["has_more"] = len(rows) > len(result["items"])
        if target.occurred_at:
            outgoing = qs.filter(direction="outbound")
            confirmed = outgoing.filter(
                delivery_status__in=["observed", "provider_accepted"], platform_message_id__isnull=False
            ).exclude(platform_message_id="")
            result["newer_outbound_observed"] = confirmed.filter(occurred_at__gt=target.occurred_at).exists()
            uncertain = outgoing.filter(Q(occurred_at__gt=target.occurred_at) | Q(occurred_at__isnull=True)).filter(
                Q(delivery_status="delivery_unverified")
                | Q(platform_message_id__isnull=True)
                | Q(platform_message_id="")
                | Q(occurred_at__isnull=True)
            )
            if not result["newer_outbound_observed"] and uncertain.exists():
                result["newer_outbound_observed"] = None
        result["ordering"] = "first_seen_at_desc"
        result["history_tool"] = "get_conversation_messages"
    return _wrap_text(result)


def _get_conversation_attachments(args: dict, context: dict[str, Any]) -> dict:
    """Page stored, sanitized metadata only; never fetch or cache media bytes."""
    key, accounts, scoped = _scope(context)
    message_id = _parse_uuid(args.get("message_id"), "message_id")
    limit = args.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise JsonRpcError(INVALID_PARAMS, "limit must be an integer between 1 and 10")
    row = ConversationMessage.objects.filter(**scoped, pk=message_id).first()
    if row is None:
        raise JsonRpcError(INVALID_PARAMS, "Conversation message not found")
    items = _safe_attachments(row.attachments, deleted=row.is_deleted, limit=None)
    scope = _cursor_scope(key, accounts, "attachments", {"message_id": str(message_id)})
    version = hashlib.sha256(json.dumps([_iso(row.updated_at), items], sort_keys=True).encode()).hexdigest()
    salt = _CURSOR_SALT + ".attachments"
    offset = 0
    if args.get("cursor") is not None:
        try:
            cursor = args["cursor"]
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                raise ValueError
            payload = signing.loads(cursor, salt=salt, max_age=_CURSOR_MAX_AGE)
            offset = payload["offset"]
            if (
                payload.get("scope") != scope
                or payload.get("version") != version
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or not 0 <= offset <= len(items)
            ):
                raise ValueError
        except (signing.BadSignature, ValueError, TypeError, KeyError) as exc:
            raise JsonRpcError(INVALID_PARAMS, "Invalid, expired, changed, or out-of-scope attachment cursor") from exc
    page: list[dict[str, Any]] = []
    for item in items[offset : offset + limit]:
        if len(json.dumps(page + [item])) > _MAX_RESPONSE_CHARS - 4096:
            break
        page.append(item)
    end = offset + len(page)
    more = end < len(items)
    return _wrap_text(
        {
            "message_id": str(row.pk),
            "items": page,
            "attachment_metadata_count": len(items),
            "has_more": more,
            "next_cursor": signing.dumps({"scope": scope, "version": version, "offset": end}, salt=salt, compress=True)
            if more
            else None,
            "observed_at": _iso(timezone.now()),
            "is_deleted": row.is_deleted,
            "content_status": _row_content_status(row),
            "media_fetched": False,
            "platform_media_complete": False,
            "note": "Stored metadata only. Links may expire or require sign-in; unavailable content is not reconstructed.",
        }
    )


_PAGING = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    "cursor": {"type": "string", "description": "Opaque cursor returned by this exact tool and scope."},
}

for _name, _description, _properties, _required, _handler in (
    (
        "list_conversations",
        "Read observed DM conversation identities for authorized accounts. Bounded and read-only; does not refresh or mark work read. History may be incomplete.",
        {"social_account_id": {"type": "string", "format": "uuid"}, **_PAGING},
        [],
        _list_conversations,
    ),
    (
        "get_conversation_messages",
        "Read bounded bidirectional observed DM history, including native outgoing, with provenance and sync coverage. Ordered by first observation, not necessarily send time. Does not authorize a reply.",
        {
            "conversation_id": {"type": "string", "format": "uuid"},
            "social_account_id": {"type": "string", "format": "uuid"},
            "unassigned_only": {
                "type": "boolean",
                "description": "Use true with social_account_id instead of conversation_id to inspect unattributed observations without guessing a thread.",
            },
            **_PAGING,
        },
        [],
        _get_conversation_messages,
    ),
    (
        "get_conversation_attachments",
        "Read all retained attachment/share metadata for an authorized V2 message in bounded pages. Does not download, "
        "cache or inspect media, infer missing group content, or claim links are readable. Use this when a history item "
        "reports attachments_truncated; attachment_metadata_count counts retained metadata, not all native platform media.",
        {
            "message_id": {"type": "string", "format": "uuid"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            "cursor": _PAGING["cursor"],
        },
        ["message_id"],
        _get_conversation_attachments,
    ),
    (
        "get_reply_context",
        "Read bounded bidirectional context for an existing inbound DM inbox ID, including sync gaps and newer observed outgoing. No observed reply does not prove nobody replied on Instagram. Read-only; does not authorize or send.",
        {"message_id": {"type": "string", "format": "uuid"}, "limit": _PAGING["limit"]},
        ["message_id"],
        _get_reply_context,
    ),
):
    register_tool(
        Tool(
            name=_name,
            description=_description,
            input_schema={
                "type": "object",
                "properties": _properties,
                "required": _required,
                "additionalProperties": False,
            },
            handler=_handler,
            enabled_setting=_FLAG,
            enabled_predicate=read_available,
        )
    )
