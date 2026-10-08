"""Current-provenance saved messages without an invented native conversation."""

from django.db.models import Q

from .canonical_access import digest, narrow_scope
from .models import ConversationMessage


def _query(scope, accounts):
    from .canonical_reads import _scope_filter

    return ConversationMessage.objects.filter(_scope_filter(scope, accounts, messages=True, unassigned=True))


def _project(row, account):
    from .canonical_reads import _iso, project_message

    value = project_message(row)
    value.update(
        source="canonical",
        conversation_id=None,
        account_name=account.account_name,
        social_account_id=str(account.pk),
        platform=account.platform,
        conversation_type="group" if row.conversation_type == "group" else "unknown",
        incoming_generation=None,
        read_tracking_available=False,
        send_authorized=False,
        observed_at=_iso(row.first_seen_at),
        assignment="unassigned",
    )
    return value


def _stable(scope, accounts, stamp, rows, expected):
    from .canonical_reads import CanonicalReadError, _recheck

    _recheck(scope, stamp)
    for row, projected in zip(rows, expected, strict=True):
        current = (
            _query(scope, accounts)
            .filter(
                pk=row.pk,
                workspace_id=row.workspace_id,
                social_account_id=row.social_account_id,
                platform=row.platform,
                platform_message_id=row.platform_message_id,
                updated_at=row.updated_at,
            )
            .first()
        )
        if current is None or digest(_project(current, accounts[current.social_account_id])) != digest(projected):
            raise CanonicalReadError("stale_revision", "The unassigned message changed; reload.")
    _recheck(scope, stamp)


def list_unassigned_messages(scope, *, social_account_id=None, platform=None, search="", cursor=None, limit=30):
    from .canonical_reads import (
        CanonicalReadError,
        _decode,
        _denied,
        _encode,
        _limit,
        _size,
        _snapshot,
        _uuid,
        _visible_content_query,
    )

    _limit(limit)
    if not isinstance(search, str) or len(search) > 500:
        raise CanonicalReadError("invalid_search", "Search must be at most 500 characters.")
    if platform is not None and platform not in {"facebook", "instagram_login"}:
        raise CanonicalReadError("invalid_filter", "Unsupported private-message platform.")
    scope = narrow_scope(
        scope,
        social_account_ids=[social_account_id] if social_account_id is not None else None,
        platforms=[platform] if platform else None,
    )
    accounts, stamp = _snapshot(scope)
    rows = _query(scope, accounts)
    if social_account_id is not None:
        account_id = _uuid(social_account_id)
        if account_id not in accounts:
            raise _denied()
        rows = rows.filter(social_account_id=account_id)
    if platform:
        rows = rows.filter(platform=platform)
    search = search.strip()
    if search:
        rows = rows.filter(_visible_content_query()).filter(
            Q(body__icontains=search)
            | Q(sender_name__icontains=search)
            | Q(social_account__account_name__icontains=search)
        )
    binding = [stamp, "unassigned", str(social_account_id), platform, search, limit]
    position = _decode(cursor, binding)
    if position:
        time, pk, lane = position
        if time is None or lane != "dated":
            raise CanonicalReadError("stale_cursor", "Invalid unassigned page.")
        rows = rows.filter(Q(first_seen_at__lt=time) | Q(first_seen_at=time, pk__lt=pk))
    candidates = list(rows.order_by("-first_seen_at", "-pk")[: limit + 1])
    items, used = [], []
    for row in candidates[:limit]:
        value = _project(row, accounts[row.social_account_id])
        if items and _size(items + [value]) > 45000:
            break
        items.append(value)
        used.append(row)
    _stable(scope, accounts, stamp, used, items)
    more = len(candidates) > len(used)
    return {
        "source": "canonical",
        "messages": items,
        "limit": limit,
        "next_cursor": _encode(binding, used[-1].first_seen_at, used[-1].pk) if more and used else None,
        "ordering": "first_observed_desc_not_send_time",
        "history_complete": False,
        "coverage": {"domain": "dm", "assignment": "unassigned", "history_complete": False},
    }


def read_unassigned_message(scope, message_id):
    from .canonical_reads import _denied, _snapshot, _uuid

    scope = narrow_scope(scope, target=(ConversationMessage, message_id))
    accounts, stamp = _snapshot(scope)
    row = _query(scope, accounts).filter(pk=_uuid(message_id)).first()
    if row is None:
        raise _denied()
    value = _project(row, accounts[row.social_account_id])
    _stable(scope, accounts, stamp, [row], [value])
    return {
        "source": "canonical",
        "message": value,
        "conversation_id": None,
        "send_authorized": False,
        "read_tracking_available": False,
        "coverage": {"domain": "dm", "assignment": "unassigned", "history_complete": False},
    }
