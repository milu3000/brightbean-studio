"""Single bounded read model for saved DM history. No provider or GET writes."""

import json
from datetime import datetime
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import BigIntegerField, Exists, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .canonical_access import (
    CanonicalReadError,
    CanonicalReadScope,
    digest,
    enabled,
    key_read_scope,
    narrow_scope,
    session_read_scope,
)
from .canonical_access import (
    denied as _denied,
)
from .canonical_access import (
    identifier as _uuid,
)
from .canonical_access import (
    recheck as _recheck,
)
from .canonical_access import (
    scope_filter as _scope_filter,
)
from .canonical_access import (
    snapshot as _snapshot,
)
from .canonical_content import WITHDRAWN_STATUSES, visible_content
from .models import ConversationMessage, ConversationReadState, ConversationSyncState, InboxConversation, InboxMessage

__all__ = [
    "CanonicalReadError",
    "CanonicalReadScope",
    "enabled",
    "session_read_scope",
    "key_read_scope",
    "list_conversations",
    "read_conversation",
    "read_message_attachments",
    "acknowledge_read",
    "available_accounts",
    "unread_conversation_count",
    "resolve_legacy_conversation",
]
SALT = "brightbean.canonical-inbox.read.v1"
_OBSERVATION_SOURCES = frozenset({"poll", "webhook", "app_send", "legacy_backfill"})
_PARTICIPANT_STATUS = {
    "participants_pair": "pair_verified",
    "participants_group": "group_observed",
    "participants_missing": "missing",
    "participants_incomplete": "incomplete",
    "participants_invalid": "invalid",
    "participant_endpoints_conflict": "conflict",
    "identity_conflict": "conflict",
}


def _iso(value):
    return value.isoformat() if value else None


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def _limit(limit, maximum=100):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise CanonicalReadError("invalid_limit", f"limit must be an integer between 1 and {maximum}")


def _encode(scope, stamp, pk, lane="dated"):
    return signing.dumps({"scope": scope, "position": [_iso(stamp), str(pk)], "lane": lane}, salt=SALT)


def _decode(token, scope):
    if token is None:
        return None
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ValueError
        value = signing.loads(token, salt=SALT, max_age=3600)
        if value["scope"] != scope:
            raise ValueError
        stamp = datetime.fromisoformat(value["position"][0]) if value["position"][0] else None
        if stamp and timezone.is_naive(stamp):
            raise ValueError
        return stamp, UUID(value["position"][1]), value["lane"]
    except (signing.BadSignature, KeyError, TypeError, ValueError, IndexError) as exc:
        raise CanonicalReadError("stale_cursor", "The page expired or its saved scope changed; reload.") from exc


def _messages(scope, accounts, conversation):
    return (
        ConversationMessage.objects.filter(
            _scope_filter(scope, accounts, messages=True),
            conversation=conversation,
            social_account_id=conversation.social_account_id,
            platform=conversation.platform,
        )
        .select_related("observation_state")
        .defer(
            "observation_state__retained_body",
            "observation_state__retained_attachments",
            "observation_state__retained_legacy_body",
            "observation_state__retained_legacy_attachments",
        )
    )


def _visible_content_query():
    from .canonical_content import canonical_legacy_restriction_query

    return (
        ~canonical_legacy_restriction_query()
        & Q(is_deleted=False)
        & ~Q(content_status__in=WITHDRAWN_STATUSES | {"expired"})
        & (
            Q(observation_state__isnull=True)
            | Q(
                observation_state__withdrawn_at__isnull=True,
                observation_state__expired_at__isnull=True,
            )
        )
    )


def _classification(row):
    """Bounded stored evidence, never reconstructed participants or send proof."""
    kind = row.conversation_type if row.conversation_type in {"direct", "group"} else "unknown"
    reason = row.classification_reason if row.classification_reason in _PARTICIPANT_STATUS else "unknown"
    if kind == "direct" and getattr(row, "peer_ambiguous", False):
        kind, reason = "unknown", "identity_conflict"
    if kind == "direct" and reason != "participants_pair":
        kind = "unknown"
    status = _PARTICIPANT_STATUS.get(reason, "unknown")
    if (reason == "participants_pair" and kind != "direct") or (reason == "participants_group" and kind != "group"):
        status = "unknown"
    return {"conversation_type": kind, "classification_reason": reason, "participants_status": status}


def project_message(row):
    """Internal projection: row must come from the exact shared scoped query."""
    content = visible_content(row, provenance_checked=True)
    body, raw_attachments = content["body"], content["attachments"]
    attachments = []
    for item in raw_attachments[:3]:
        value = {
            key: item.get(key, "")
            for key in ("type", "url", "preview_url", "title", "availability", "availability_reason")
        }
        value["title"] = str(value["title"])[:500]
        for key in ("url", "preview_url"):
            if len(str(value[key])) > 2000:
                value[key], value["availability"], value["availability_reason"] = "", "unavailable", "size_limited"
        attachments.append(value)
    result = {
        "id": str(row.pk),
        "id_namespace": "canonical_message",
        "conversation_id": str(row.conversation_id) if row.conversation_id else None,
        "platform_message_id": row.platform_message_id or None,
        "social_account_id": str(row.social_account_id),
        "platform": row.platform,
        **_classification(row),
        "source": "canonical",
        "persisted": True,
        "sources": sorted({value for value in row.sources if isinstance(value, str) and value in _OBSERVATION_SOURCES})
        if isinstance(row.sources, list)
        else [],
        "first_seen_at": _iso(row.first_seen_at),
        "updated_at": _iso(row.updated_at),
        "direction": row.direction,
        "incoming_generation": row.incoming_generation,
        "sender_name": row.sender_name[:255],
        "body": body[:4000],
        "body_truncated": len(body) > 4000,
        "attachments": attachments,
        "attachments_truncated": len(raw_attachments) > len(attachments),
        "attachment_metadata_count": len(raw_attachments),
        "content_type": "mixed"
        if body and attachments
        else "attachment"
        if attachments
        else "text"
        if body
        else "unknown",
        "content_status": content["content_status"],
        "content_available": content["available"] and bool(body or any(item.get("url") for item in raw_attachments)),
        "content_completeness": "unavailable"
        if not content["available"]
        else "partial"
        if content["content_status"] in {"partial", "fields_unavailable", "unsupported"}
        else "unknown",
        "media_fetched": False,
        "platform_media_complete": False,
        "is_deleted": content["is_deleted"],
        "is_expired": content["is_expired"],
        "occurred_at": _iso(row.occurred_at),
        "timestamp_missing": row.occurred_at is None,
        "delivery_status": row.delivery_status,
        "legacy_message_id": str(row.legacy_message_id) if row.legacy_message_id else None,
        "legacy_reply_id": str(row.legacy_reply_id) if row.legacy_reply_id else None,
    }
    while _size(result) > 10000 and result["attachments"]:
        result["attachments"].pop()
        result["attachments_truncated"] = True
    while _size(result) > 10000 and result["body"]:
        result["body"] = result["body"][: len(result["body"]) // 2]
        result["body_truncated"] = True
    if content["available"] and (result["body_truncated"] or result["attachments_truncated"]):
        result["content_completeness"] = "partial"
    return result


def _read_state(conversation, user_id):
    generation = (
        ConversationReadState.objects.filter(conversation=conversation, user_id=user_id)
        .values_list("read_generation", flat=True)
        .first()
        or 0
    )
    return {
        "read_generation": generation,
        "incoming_generation": conversation.incoming_generation,
        "unread": conversation.incoming_generation > generation,
    }


def _identity(conversation):
    return [
        str(conversation.workspace_id),
        str(conversation.social_account_id),
        conversation.platform,
        conversation.platform_conversation_id,
        conversation.peer_id,
        conversation.peer_ambiguous,
        conversation.conversation_type,
    ]


def _conversation(scope, accounts, row):
    account = accounts[row.social_account_id]
    messages = _messages(scope, accounts, row)
    latest = messages.filter(occurred_at__isnull=False).order_by("-occurred_at", "-pk").first()
    inbound = messages.filter(_visible_content_query(), direction="inbound")
    name = (
        inbound.exclude(sender_name="")
        .order_by(F("occurred_at").desc(nulls_last=True), "-pk")
        .values_list("sender_name", flat=True)
        .first()
    )
    anchor = (
        inbound.filter(legacy_message__isnull=False)
        .order_by(F("occurred_at").desc(nulls_last=True), "-pk")
        .values_list("legacy_message_id", flat=True)
        .first()
    )
    classification = _classification(row)
    peer_name = (
        (name or row.peer_id)
        if classification["conversation_type"] == "direct"
        else "Group conversation"
        if classification["conversation_type"] == "group"
        else "Conversation (type unknown)"
    )
    return {
        "id": str(row.pk),
        "id_namespace": "canonical_conversation",
        "platform_conversation_id": row.platform_conversation_id or None,
        "identity_kind": row.identity_kind,
        "source": "canonical",
        "persisted": True,
        "workspace_id": str(row.workspace_id),
        "social_account_id": str(row.social_account_id),
        "platform": row.platform,
        "domain": "dm",
        "account_name": account.account_name,
        "account_handle": account.account_handle,
        "peer_name": peer_name,
        **classification,
        "sync": _coverage(account),
        "revision": row.revision,
        "incoming_generation": row.incoming_generation,
        "workflow_state": row.workflow_state,
        "workflow_order_uncertain": bool(row.workflow_order_uncertain),
        "workflow_tracking_available": bool(
            getattr(settings, "INBOX_CONVERSATION_WORKFLOW_ENABLED", False) is True and row.workflow_baseline_at
        ),
        "legacy_anchor_id": str(anchor) if anchor else None,
        "latest_message": project_message(latest) if latest else None,
        "latest_activity_at": _iso(latest.occurred_at) if latest else None,
        "has_undated_messages": messages.filter(occurred_at__isnull=True).exists(),
        "read_tracking_available": True,
        "read_state": _read_state(row, scope.user_id),
        "archived": account._canonical_archive is not None,
        "send_authorized": False,
    }


def _coverage(account):
    state = ConversationSyncState.objects.filter(
        workspace_id=account.workspace_id, social_account=account, platform=account.platform, stream="dm"
    ).first()
    return {
        "domain": "dm",
        "scope": "account_dm_stream",
        "source": "legacy_poll_stream",
        "status": state.status if state else "unknown",
        "coverage": state.coverage if state else "unknown",
        "last_attempt_at": _iso(state.last_attempt_at) if state else None,
        "last_success_at": _iso(state.last_success_at) if state else None,
        "conversation_freshness": "unknown",
        "history_complete": False,
        "unsupported_domains": ["comment", "mention", "review"],
    }


def available_accounts(scope):
    accounts, token = _snapshot(scope)
    result = [
        {
            "id": str(item.pk),
            "platform": item.platform,
            "account_name": item.account_name,
            "account_handle": item.account_handle,
        }
        for item in accounts.values()
    ]
    _recheck(scope, token)
    return result


def _message_subquery(scope, accounts):
    return ConversationMessage.objects.filter(
        _scope_filter(scope, accounts, messages=True),
        conversation_id=OuterRef("pk"),
        social_account_id=OuterRef("social_account_id"),
        platform=OuterRef("platform"),
    )


def unread_conversation_count(scope):
    accounts, token = _snapshot(scope)
    progress = ConversationReadState.objects.filter(conversation_id=OuterRef("pk"), user_id=scope.user_id)
    count = (
        InboxConversation.objects.filter(_scope_filter(scope, accounts))
        .annotate(
            has_messages=Exists(_message_subquery(scope, accounts)),
            user_read=Coalesce(
                Subquery(progress.values("read_generation")[:1]), Value(0), output_field=BigIntegerField()
            ),
        )
        .filter(has_messages=True, incoming_generation__gt=F("user_read"))
        .count()
    )
    _recheck(scope, token)
    return count


def list_conversations(
    scope, *, social_account_id=None, platform=None, workflow_state=None, search="", cursor=None, limit=30
):
    from .canonical_compat import inbox_sources

    scope = narrow_scope(
        scope,
        social_account_ids=[social_account_id] if social_account_id is not None else None,
        platforms=[platform] if platform else None,
    )
    sources = inbox_sources(scope, social_account_ids=[social_account_id] if social_account_id is not None else None)
    _limit(limit)
    if not isinstance(search, str) or len(search) > 500:
        raise CanonicalReadError("invalid_search", "search must be at most 500 characters")
    if platform is not None and platform not in {"facebook", "instagram_login"}:
        raise CanonicalReadError("invalid_filter", "This canonical DM adapter does not support that platform.")
    if workflow_state is not None and workflow_state not in {"needs_action", "waiting", "done", "unclassified"}:
        raise CanonicalReadError("invalid_filter", "Unknown workflow state")
    try:
        accounts, token = _snapshot(scope)
    except CanonicalReadError as exc:
        if exc.code == "canonical_unavailable":
            exc.data = {**(exc.data or {}), "account_sources": sources}
        raise
    query = InboxConversation.objects.filter(_scope_filter(scope, accounts))
    if social_account_id is not None:
        social_account_id = _uuid(social_account_id)
        if social_account_id not in accounts:
            raise _denied()
        query = query.filter(social_account_id=social_account_id)
    if platform:
        query = query.filter(platform=platform)
    if workflow_state:
        query = query.filter(workflow_state=None if workflow_state == "unclassified" else workflow_state)
    messages = _message_subquery(scope, accounts)
    query = query.annotate(has_messages=Exists(messages)).filter(has_messages=True)
    search = search.strip()
    if search:
        matching = messages.filter(_visible_content_query()).filter(
            Q(body__icontains=search) | Q(direction="inbound", sender_name__icontains=search)
        )
        query = query.annotate(has_match=Exists(matching)).filter(
            Q(has_match=True)
            | Q(peer_id__icontains=search)
            | Q(social_account__account_name__icontains=search)
            | Q(social_account__account_handle__icontains=search)
        )
    revision = digest(
        list(
            query.order_by("pk").values_list(
                "pk",
                "workspace_id",
                "social_account_id",
                "platform",
                "platform_conversation_id",
                "peer_id",
                "peer_ambiguous",
                "conversation_type",
                "revision",
                "incoming_generation",
                "workflow_state",
            )
        )
    )
    binding = [token, "list", str(social_account_id), platform, workflow_state, search, revision, limit]
    position = _decode(cursor, binding)
    query = query.annotate(
        latest_at=Subquery(
            messages.filter(occurred_at__isnull=False).order_by("-occurred_at", "-pk").values("occurred_at")[:1]
        )
    )
    if position:
        stamp, pk, lane = position
        if lane != "dated":
            raise CanonicalReadError("stale_cursor", "Invalid list cursor")
        query = (
            query.filter(Q(latest_at__lt=stamp) | Q(latest_at=stamp, pk__lt=pk) | Q(latest_at__isnull=True))
            if stamp
            else query.filter(latest_at__isnull=True, pk__lt=pk)
        )
    rows = list(query.order_by(F("latest_at").desc(nulls_last=True), "-pk")[: limit + 1])
    items, consumed = [], []
    for row in rows[:limit]:
        item = _conversation(scope, accounts, row)
        if items and 2 * _size(items + [item]) > 52000:
            break
        items.append(item)
        consumed.append(row)
    _recheck(scope, token)
    current = InboxConversation.objects.filter(_scope_filter(scope, accounts), pk__in=[row.pk for row in consumed])
    if set(current.values_list("pk", "revision", "incoming_generation")) != {
        (row.pk, row.revision, row.incoming_generation) for row in consumed
    }:
        raise CanonicalReadError("stale_revision", "Conversation history changed while reading; reload.")
    if (
        inbox_sources(scope, social_account_ids=[social_account_id] if social_account_id is not None else None)
        != sources
    ):
        raise CanonicalReadError("stale_scope", "The current inbox sources changed; reload.")
    more = len(rows) > len(consumed)
    return {
        "source": "canonical",
        "account_sources": sources,
        "persisted": True,
        "conversations": items,
        "items": items,
        "limit": limit,
        "next_cursor": _encode(binding, consumed[-1].latest_at, consumed[-1].pk) if more and consumed else None,
        "ordering": "latest_known_occurrence_desc_unknown_last",
        "history_complete": False,
        "coverage": {"domain": "dm", "unsupported_domains": ["comment", "mention", "review"]},
    }


def _notifications(scope, conversation, seen_generation):
    from apps.notifications.models import Notification

    return Notification.objects.filter(
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        conversation=conversation,
        event_type="new_inbox_message",
        superseded_by__isnull=True,
        source_revision__gt=0,
        source_revision__lte=seen_generation,
    )


def read_conversation(scope, conversation_id, *, cursor=None, limit=30):
    _limit(limit)
    scope = narrow_scope(scope, target=(InboxConversation, conversation_id))
    accounts, token = _snapshot(scope)
    row = InboxConversation.objects.filter(_scope_filter(scope, accounts), pk=_uuid(conversation_id)).first()
    if row is None:
        raise _denied()
    identity, revision, generation = _identity(row), row.revision, row.incoming_generation
    binding = [token, "timeline", str(row.pk), identity, limit]
    position = _decode(cursor, binding)
    lane = position[2] if position else "dated"
    if lane not in {"dated", "undated"}:
        raise CanonicalReadError("stale_cursor", "Invalid timeline cursor")
    messages = _messages(scope, accounts, row)
    dated = messages.filter(occurred_at__isnull=False)
    undated = messages.filter(occurred_at__isnull=True)
    if position:
        stamp, pk, _lane = position
        if lane == "dated":
            if stamp is None:
                raise CanonicalReadError("stale_cursor", "A chronological position is required")
            dated = dated.filter(Q(occurred_at__lt=stamp) | Q(occurred_at=stamp, pk__lt=pk))
        else:
            if stamp is not None:
                raise CanonicalReadError("stale_cursor", "Undated messages have no chronological position")
            undated = undated.filter(pk__lt=pk)
    conversation = _conversation(scope, accounts, row)
    result = {
        "source": "canonical",
        "persisted": True,
        "conversation": conversation,
        "messages": [],
        "undated_messages": [],
        "next_cursor": None,
        "undated_next_cursor": None,
        "history_complete": False,
        "ordering": "occurred_at_asc",
        "limit": limit,
        "coverage": conversation["sync"],
    }
    unknown_limit = limit if lane == "undated" else min(limit, 5)
    pages = [
        (
            list(dated.order_by("-occurred_at", "-pk")[: limit + 1]) if lane == "dated" else [],
            "messages",
            limit,
            "dated",
        ),
        (
            list(undated.order_by("-pk")[: unknown_limit + 1]) if cursor is None or lane == "undated" else [],
            "undated_messages",
            unknown_limit,
            "undated",
        ),
    ]
    for records, field, count, page_lane in pages:
        consumed = []
        for record in records[:count]:
            value = project_message(record)
            if consumed and _size(result[field] + [value]) > 20000:
                break
            result[field].append(value)
            consumed.append(record)
        if consumed and len(records) > len(consumed):
            last = consumed[-1]
            result["next_cursor" if page_lane == "dated" else "undated_next_cursor"] = _encode(
                binding, last.occurred_at, last.pk, page_lane
            )
    result["messages"].reverse()
    seen = [
        item["incoming_generation"]
        for item in result["messages"] + result["undated_messages"]
        if item["direction"] == "inbound" and item["incoming_generation"] is not None
    ]
    # A rendered tombstone can be acknowledged without ever exposing its body.
    result["read_ack_token"] = (
        signing.dumps(
            {
                "scope": token,
                "conversation": str(row.pk),
                "identity": identity,
                "revision": revision,
                "seen_generation": max(seen),
                "notifications": [
                    [str(pk), rev]
                    for pk, rev in _notifications(scope, row, max(seen))
                    .order_by("pk")
                    .values_list("pk", "revision")[:20]
                ],
            },
            salt=SALT + ".read-ack",
        )
        if seen
        else None
    )
    _recheck(scope, token)
    current = InboxConversation.objects.filter(_scope_filter(scope, accounts), pk=row.pk).first()
    if current is None or (_identity(current), current.revision, current.incoming_generation) != (
        identity,
        revision,
        generation,
    ):
        raise CanonicalReadError("stale_revision", "Conversation history changed while reading; reload.")
    from .composer_observation import issue_observation

    result["composer_observation_token"] = (
        issue_observation(token, row, result["messages"] + result["undated_messages"]) if cursor is None else None
    )
    return result


@transaction.atomic
def acknowledge_read(scope, conversation_id, read_ack_token):
    from .locking import lock_dm_account

    scope = narrow_scope(scope, target=(InboxConversation, conversation_id))
    accounts, token = _snapshot(scope)
    selected = InboxConversation.objects.filter(_scope_filter(scope, accounts), pk=_uuid(conversation_id)).first()
    if selected is None or lock_dm_account(selected.social_account_id, scope.workspace_id) is None:
        raise _denied()
    current = (
        InboxConversation.objects.select_for_update()
        .filter(
            pk=selected.pk,
            workspace_id=scope.workspace_id,
            social_account_id=selected.social_account_id,
            platform=selected.platform,
        )
        .first()
    )
    _recheck(scope, token)
    try:
        if not isinstance(read_ack_token, str) or not 1 <= len(read_ack_token) <= 4096 or current is None:
            raise ValueError
        data = signing.loads(read_ack_token, salt=SALT + ".read-ack", max_age=3600)
        generation, revision = data["seen_generation"], data["revision"]
        notifications = data.get("notifications", [])
        if (
            data["scope"] != token
            or data["conversation"] != str(current.pk)
            or data["identity"] != _identity(current)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 0 <= generation <= current.incoming_generation
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 0 <= revision <= current.revision
            or not isinstance(notifications, list)
            or len(notifications) > 20
            or not InboxConversation.objects.filter(_scope_filter(scope, accounts), pk=current.pk).exists()
        ):
            raise ValueError
    except (signing.BadSignature, ValueError, TypeError, KeyError) as exc:
        raise CanonicalReadError("stale_cursor", "The read acknowledgement changed or expired; reload.") from exc
    state, _ = ConversationReadState.objects.get_or_create(conversation=current, user_id=scope.user_id)
    if generation > state.read_generation:
        state.read_generation = generation
        state.save(update_fields=["read_generation", "updated_at"])
    notified = 0
    for pk, rev in notifications:
        notified += (
            _notifications(scope, current, generation)
            .filter(pk=pk, revision=rev)
            .update(is_read=True, read_at=timezone.now(), read_revision=rev)
        )
    _recheck(scope, token)
    return {
        "source": "canonical",
        "conversation_id": str(current.pk),
        "read_state": _read_state(current, scope.user_id),
        "workflow_state": current.workflow_state,
        "notifications_marked_read": notified,
    }


def _legacy_canonical_messages(scope, accounts, legacy):
    """Read-only identity bridge; never create or repair a legacy link.

    Native IDs are scoped to one account/platform. An unlinked row additionally
    needs saved connection/archive provenance and cannot override another row's
    explicit legacy link. Neither content nor timestamps establish identity.
    """
    account = accounts.get(legacy.social_account_id)
    if account is None or not legacy.platform_message_id:
        return ConversationMessage.objects.none()
    matching = Q(legacy_message_id=legacy.pk)
    if account._canonical_connection is not None or account._canonical_archive is not None:
        linked = ConversationMessage.objects.filter(legacy_message_id=legacy.pk)
        matching |= Q(legacy_message__isnull=True) & ~Q(Exists(linked))
    current = InboxMessage.objects.filter(
        pk=legacy.pk,
        workspace_id=scope.workspace_id,
        social_account_id=account.pk,
        social_account__workspace_id=scope.workspace_id,
        social_account__platform=account.platform,
        platform_message_id=legacy.platform_message_id,
        message_type="dm",
    )
    return ConversationMessage.objects.filter(
        _scope_filter(scope, accounts, messages=True),
        matching,
        Exists(current),
        social_account_id=account.pk,
        platform=account.platform,
        platform_message_id=legacy.platform_message_id,
        direction="inbound",
        legacy_reply__isnull=True,
        conversation__workspace_id=scope.workspace_id,
        conversation__social_account_id=account.pk,
        conversation__platform=account.platform,
    )


def resolve_legacy_conversation(scope, message_id):
    scope = narrow_scope(scope, target=(InboxMessage, message_id))
    accounts, token = _snapshot(scope)
    legacy = InboxMessage.objects.filter(
        pk=_uuid(message_id),
        workspace_id=scope.workspace_id,
        social_account_id__in=accounts,
        social_account__workspace_id=scope.workspace_id,
        message_type="dm",
    ).first()
    if legacy is None:
        raise _denied()
    matching = _legacy_canonical_messages(scope, accounts, legacy)
    row = matching.first()
    if row is None or row.conversation_id is None:
        raise CanonicalReadError(
            "canonical_unavailable", "This incoming record has no proven canonical conversation yet."
        )
    if not InboxConversation.objects.filter(
        _scope_filter(scope, accounts), pk=row.conversation_id, social_account_id=legacy.social_account_id
    ).exists():
        raise _denied()
    _recheck(scope, token)
    if not matching.filter(pk=row.pk, conversation_id=row.conversation_id, updated_at=row.updated_at).exists():
        raise CanonicalReadError("stale_revision", "The incoming message changed while reading; reload.")
    return row.conversation_id


def read_message_attachments(scope, message_id, *, cursor=None, limit=3, unassigned=False):
    _limit(limit, 10)
    scope = narrow_scope(scope, target=(ConversationMessage, message_id))
    accounts, token = _snapshot(scope)
    row = ConversationMessage.objects.filter(
        _scope_filter(scope, accounts, messages=True, unassigned=unassigned),
        pk=_uuid(message_id),
        conversation__isnull=unassigned,
    ).first()
    if row is None:
        raise _denied()
    conversation = InboxConversation.objects.filter(_scope_filter(scope, accounts), pk=row.conversation_id).first()
    if conversation is None and not unassigned:
        raise _denied()
    content = visible_content(row)
    attachments = content["attachments"]
    binding = [
        token,
        "attachments",
        str(row.pk),
        _identity(conversation) if conversation else ["unassigned", str(row.social_account_id), row.platform],
        [
            str(row.social_account_id),
            row.platform,
            row.platform_message_id,
            str(row.legacy_message_id),
            str(row.legacy_reply_id),
        ],
        row.updated_at.isoformat(),
        digest(attachments),
        limit,
    ]
    offset = 0
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                raise ValueError
            data = signing.loads(cursor, salt=SALT, max_age=3600)
            offset = data["offset"]
            if (
                data["scope"] != binding
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or not 0 <= offset <= len(attachments)
            ):
                raise ValueError
        except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
            raise CanonicalReadError("stale_cursor", "The attachment page changed or expired.") from exc
    items = []
    for attachment in attachments[offset : offset + limit]:
        item = {
            key: attachment.get(key, "")
            for key in ("type", "url", "preview_url", "title", "availability", "availability_reason")
        }
        item["title"] = str(item["title"])[:500]
        for key in ("url", "preview_url"):
            if len(str(item[key])) > 2000:
                item[key], item["availability"], item["availability_reason"] = "", "unavailable", "size_limited"
        if items and _size(items + [item]) > 30000:
            break
        items.append(item)
    _recheck(scope, token)
    if not ConversationMessage.objects.filter(
        _scope_filter(scope, accounts, messages=True, unassigned=unassigned),
        pk=row.pk,
        updated_at=row.updated_at,
        social_account_id=row.social_account_id,
        platform=row.platform,
        platform_message_id=row.platform_message_id,
    ).exists() or (
        conversation is not None
        and not InboxConversation.objects.filter(
            _scope_filter(scope, accounts), pk=conversation.pk, revision=conversation.revision
        ).exists()
    ):
        raise CanonicalReadError("stale_revision", "The message changed while reading.")
    end = offset + len(items)
    return {
        "source": "canonical",
        "message_id": str(row.pk),
        "items": items,
        "attachment_metadata_count": len(attachments),
        "is_deleted": content["is_deleted"],
        "is_expired": content["is_expired"],
        "media_fetched": False,
        "platform_media_complete": False,
        "has_more": end < len(attachments),
        "next_cursor": signing.dumps({"scope": binding, "offset": end}, salt=SALT) if end < len(attachments) else None,
    }


# Stable adapter names for the UI/REST/MCP callers; lazy imports avoid cycles.
def hold_legacy_fallback(*args, **kwargs):
    from .canonical_compat import hold_legacy_fallback as adapter

    return adapter(*args, **kwargs)


def read_legacy_thread(*args, **kwargs):
    from .canonical_compat import read_legacy_thread as adapter

    return adapter(*args, **kwargs)


def read_legacy_message(*args, **kwargs):
    from .canonical_compat import read_legacy_message as adapter

    return adapter(*args, **kwargs)


def read_canonical_incoming_message(*args, **kwargs):
    from .canonical_compat import read_canonical_incoming_message as adapter

    return adapter(*args, **kwargs)


def list_legacy_dm_adapter(*args, **kwargs):
    from .canonical_compat import list_legacy_dm_adapter as adapter

    return adapter(*args, **kwargs)


def recheck_legacy_list(scope, expected, *, social_account_id=None):
    from .canonical_compat import recheck_inbox_source

    return recheck_inbox_source(
        scope, expected, social_account_ids=[social_account_id] if social_account_id is not None else None
    )


def read_message_body(*args, **kwargs):
    from .canonical_read_details import read_message_body as read

    return read(*args, **kwargs)


def verify_composer_observation(*args, **kwargs):
    from .composer_observation import verify_composer_observation as verify

    return verify(*args, **kwargs)


def list_unassigned_messages(*args, **kwargs):
    from .unassigned_reads import list_unassigned_messages as read

    return read(*args, **kwargs)


def read_unassigned_message(*args, **kwargs):
    from .unassigned_reads import read_unassigned_message as read

    return read(*args, **kwargs)


def read_unassigned_message_body(scope, message_id, *, cursor=None, limit=2000):
    from .canonical_read_details import read_message_body

    return read_message_body(scope, message_id, cursor=cursor, limit=limit, unassigned=True)


def read_unassigned_message_attachments(scope, message_id, *, cursor=None, limit=3):
    return read_message_attachments(scope, message_id, cursor=cursor, limit=limit, unassigned=True)
