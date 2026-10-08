"""Bounded, transient pages of an already-scoped native conversation.

Each call reads one provider page. This module does not send, refresh credentials, cache, capture,
mark read, or reconcile replies. A platform observation is never a receipt
or a grant to send. The usual automated reply window remains unchanged.
"""

import base64
import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime

from apps.members.models import WorkspaceMembership
from providers.meta_inbox_content import (
    CONTENT_MESSAGE_FIELDS,
    MAX_ATTACHMENTS,
    _identity_ids,
    classify_conversation_identity,
    is_deleted_content,
    message_content_status,
    normalize_attachments,
)

from .locking import lock_dm_account
from .models import ConversationMessage, InboxMessage
from .presentation import native_thread_id, stored_thread_messages, thread_id

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 30 * 1024  # Also leaves room for MCP's JSON-in-text envelope.
MAX_BODY_CHARACTERS = 10000
MAX_ITEMS = 100
PROVIDER_PAGE_LIMIT = 20
MAX_PROVIDER_CURSOR_LENGTH = 1024
MAX_CONTINUATION_LENGTH = 6144
CONTINUATION_MAX_AGE = 900
CONTINUATION_SALT = "inbox.native-history.older.v1"
_MESSAGE_FIELDS = CONTENT_MESSAGE_FIELDS + ",to{id}"
_CONFLICT_REASONS = {
    "identity_conflict",
    "participants_invalid",
    "participants_incomplete",
    "participant_endpoints_conflict",
}


class NativeThreadReadError(Exception):
    """A stable, non-provider diagnostic suitable for the read surfaces."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _denied():
    return NativeThreadReadError("authorization_revoked", "Current permission to read this inbox is unavailable.")


def session_read_authorization(user):
    user_id = getattr(user, "pk", None)

    def authorize(account):
        member = (
            WorkspaceMembership.objects.select_related("custom_role", "workspace")
            .filter(
                user_id=user_id, user__is_active=True, workspace_id=account.workspace_id, workspace__is_archived=False
            )
            .first()
        )
        if (
            member is None
            or (member.custom_role_id and member.custom_role.organization_id != member.workspace.organization_id)
            or member.effective_permissions.get("use_inbox", False) is not True
        ):
            raise _denied()

    authorize.native_read_context = ("session", str(user_id))
    return authorize


def key_read_authorization(api_key, request=None):
    """Revalidate current grants and the original credential on both sides of HTTP."""
    workspace_id, actor_id = api_key.workspace_id, api_key.issued_by_id
    oauth = getattr(api_key, "is_oauth", False)
    key_id = None if oauth else api_key.pk
    header = request.headers.get("Authorization", "") if request is not None else ""
    credential_identity = None if oauth else (api_key.token_hash, api_key.lookup_prefix)
    user_authorization = session_read_authorization(api_key.issued_by)

    def authorize(account):
        from apps.api.auth import _resolve_oauth_actor
        from apps.api_keys.models import ApiKey

        if account.workspace_id != workspace_id or actor_id is None:
            raise _denied()
        if oauth:
            if request is None or request.headers.get("Authorization", "") != header:
                raise _denied()
            current = _resolve_oauth_actor(header[7:]) if header.startswith("Bearer ") else None
            permitted = (
                current is not None
                and current.workspace_id == workspace_id
                and current.issued_by_id == actor_id
                and current.effective_permissions.get("use_inbox", False) is True
                and current.social_accounts.all().filter(pk=account.pk, workspace_id=workspace_id).exists()
            )
        else:
            current = ApiKey.objects.filter(pk=key_id, workspace_id=workspace_id, issued_by_id=actor_id).first()
            permitted = (
                current is not None
                and current.is_active
                and (current.token_hash, current.lookup_prefix) == credential_identity
                and isinstance(current.permissions, list)
                and "use_inbox" in current.permissions
                and current.social_accounts.filter(pk=account.pk, workspace_id=workspace_id).exists()
            )
        if not permitted:
            raise _denied()
        user_authorization(account)

    authorize.native_read_context = (
        "oauth" if oauth else "api_key",
        str(workspace_id),
        str(actor_id),
        str(key_id),
        salted_hmac(CONTINUATION_SALT, header if oauth else repr(credential_identity)).hexdigest(),
    )
    return authorize


def _anchor_identity(message):
    return deepcopy(
        (
            message.pk,
            message.workspace_id,
            message.social_account_id,
            message.message_type,
            message.platform_message_id,
            message.sender_handle,
            message.received_at,
            message.extra,
        )
    )


def _selected(original, authorization):
    if authorization is None or not callable(authorization):
        raise NativeThreadReadError("authorization_required", "Current inbox read authorization is required.")
    account = lock_dm_account(original.social_account_id, original.workspace_id)
    message = (
        InboxMessage.objects.select_for_update()
        .filter(pk=original.pk, workspace_id=original.workspace_id, social_account_id=original.social_account_id)
        .first()
    )
    if account is None or message is None or _anchor_identity(message) != _anchor_identity(original):
        raise NativeThreadReadError("stale", "The selected conversation changed; reload before checking.")
    authorization(account)
    message.social_account = account
    return account, message


def _canonical(message):
    # Like the shared reply barrier, inspect existing identity conflicts even
    # while optional history reads are disabled. Do not select ledger bodies,
    # attachments or content metadata, and never present ledger history here.
    row = (
        ConversationMessage.objects.select_related("conversation")
        .only(
            "pk",
            "workspace_id",
            "social_account_id",
            "platform",
            "platform_message_id",
            "direction",
            "sender_id",
            "recipient_id",
            "is_deleted",
            "conversation_type",
            "classification_reason",
            "conversation_id",
            "conversation__workspace_id",
            "conversation__social_account_id",
            "conversation__platform",
            "conversation__platform_conversation_id",
            "conversation__peer_id",
            "conversation__peer_ambiguous",
            "conversation__conversation_type",
            "conversation__classification_reason",
            "conversation__revision",
        )
        .filter(legacy_message_id=message.pk)
        .first()
    )
    if row is None:
        return None, None
    # This only reads existing optional evidence; it never enables capture.
    identity = (
        row.pk,
        row.workspace_id,
        row.social_account_id,
        row.platform,
        row.platform_message_id,
        row.direction,
        row.sender_id,
        row.recipient_id,
        row.is_deleted,
        row.conversation_type,
        row.classification_reason,
        row.conversation_id,
    )
    if row.conversation_id:
        conversation = row.conversation
        identity += (
            conversation.workspace_id,
            conversation.social_account_id,
            conversation.platform,
            conversation.platform_conversation_id,
            conversation.peer_id,
            conversation.peer_ambiguous,
            conversation.conversation_type,
            conversation.classification_reason,
            conversation.revision,
        )
    return row, identity


def _identity(account, message, canonical_identity):
    return deepcopy(
        (
            account.pk,
            account.workspace_id,
            account.platform,
            account.account_platform_id,
            account.webhook_target_id,
            account.oauth_access_token,
            account.token_expires_at,
            account.connection_status,
            account.analytics_auth_updated_at,
            account.missing_scopes,
            _anchor_identity(message),
            canonical_identity,
        )
    )


def _peer(account, message, canonical):
    extra = message.extra if isinstance(message.extra, dict) else {}
    own = {native_thread_id(account.account_platform_id), native_thread_id(account.webhook_target_id)} - {""}
    if not native_thread_id(account.account_platform_id):
        return ""
    values = []
    for key in ("recipient_id", "sender_id", "sender", "from"):
        if key in extra:
            value = extra[key]
            values.append(value.get("id") if isinstance(value, dict) else value)
    values = [value for value in values if value != "" and value is not None]
    if not values:
        values = [message.sender_handle]
    ids = [native_thread_id(value) for value in values]
    if not ids or any(not value for value in ids) or len(set(ids)) != 1 or ids[0] in own:
        return ""
    peer = ids[0]
    nested = extra.get("message") if isinstance(extra.get("message"), dict) else {}
    if (
        extra.get("direction") == "outbound"
        or extra.get("is_echo")
        or extra.get("is_self")
        or nested.get("is_echo")
        or is_deleted_content(extra)
        or extra.get("conversation_type") == "group"
        or (
            extra.get("conversation_type") == "unknown"
            and extra.get("classification_reason") not in (None, "", "participants_missing")
        )
        or (isinstance(extra.get("classification_reason"), str) and extra["classification_reason"] in _CONFLICT_REASONS)
    ):
        return ""
    if any(key in extra for key in ("participants", "participant_ids")):
        kind, _reason, verified_peer = classify_conversation_identity(extra, own_ids=own, sender_id=peer)
        if kind != "direct" or verified_peer != peer:
            return ""
    elif extra.get("conversation_type") not in (None, "", "direct", "unknown") or extra.get(
        "classification_reason"
    ) not in (None, "", "participants_pair", "participants_missing"):
        return ""
    # Older polling stored native thread + peer endpoints without participants.
    # That is enough to address this exact read, never enough to return content:
    # _project still requires the provider's complete own+peer participant pair.
    if canonical is not None:
        if (
            canonical.workspace_id != message.workspace_id
            or canonical.social_account_id != account.pk
            or canonical.platform != account.platform
            or canonical.platform_message_id != message.platform_message_id
            or canonical.direction != "inbound"
            or canonical.is_deleted
            or canonical.sender_id != peer
            or canonical.recipient_id not in own
            or canonical.conversation_type != "direct"
            or canonical.classification_reason in _CONFLICT_REASONS
        ):
            return ""
        if canonical.conversation_id:
            conversation = canonical.conversation
            if (
                conversation.workspace_id != message.workspace_id
                or conversation.social_account_id != account.pk
                or conversation.platform != account.platform
                or conversation.peer_id != peer
                or conversation.peer_ambiguous
                or conversation.conversation_type != "direct"
                or conversation.classification_reason in _CONFLICT_REASONS
                or conversation.platform_conversation_id not in (None, "", thread_id(message))
            ):
                return ""
    return peer


def _sibling_evidence(account, message, peer):
    """Pin only safety evidence for this exact native thread, never history content."""
    columns = (
        "pk",
        "extra__conversation_type",
        "extra__classification_reason",
        "extra__participants",
        "extra__participant_ids",
        "conversation_message__conversation_type",
        "conversation_message__classification_reason",
        "conversation_message__conversation__conversation_type",
        "conversation_message__conversation__classification_reason",
        "conversation_message__conversation__peer_ambiguous",
        "conversation_message__conversation__peer_id",
    )
    digest, conflicting = hashlib.sha256(), False
    own = {account.account_platform_id, account.webhook_target_id} - {""}
    siblings = stored_thread_messages(message).exclude(pk=message.pk).order_by("pk").values(*columns)
    for row in siblings.iterator(chunk_size=200):
        digest.update(json.dumps(row, sort_keys=True, default=str).encode())
        for prefix in ("extra__", "conversation_message__", "conversation_message__conversation__"):
            reason = row[prefix + "classification_reason"]
            if row[prefix + "conversation_type"] == "group" or (
                isinstance(reason, str) and reason in _CONFLICT_REASONS
            ):
                conflicting = True
        canonical_peer = row["conversation_message__conversation__peer_id"]
        if row["conversation_message__conversation__peer_ambiguous"] or (canonical_peer and canonical_peer != peer):
            conflicting = True
        participants = {
            key: row["extra__" + key] for key in ("participants", "participant_ids") if row["extra__" + key] is not None
        }
        if participants:
            kind, _reason, verified_peer = classify_conversation_identity(participants, own_ids=own, sender_id=peer)
            if kind != "direct" or verified_peer != peer:
                conflicting = True
    return digest.digest(), conflicting


class _ProviderReadError(Exception):
    def __init__(self, code):
        self.code = code


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _provider_json(client, account, path, params):
    """A bounded GET without provider diagnostics, redirects or retries."""
    with client.stream(
        "GET",
        path,
        headers={"Authorization": f"Bearer {account.oauth_access_token}"},
        params=params,
    ) as response:
        if response.status_code == 429:
            raise _ProviderReadError("rate_limited")
        if response.status_code in {401, 403}:
            raise _ProviderReadError("platform_permission_unavailable")
        if not 200 <= response.status_code < 300:
            raise _ProviderReadError("provider_unavailable")
        content = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                raise _ProviderReadError("response_too_large")
            content.extend(chunk)
        try:
            return json.loads(content, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise _ProviderReadError("invalid_response") from None


def _verified_participants(data, account, native_id, peer):
    if not isinstance(data, dict):
        raise _ProviderReadError("invalid_response")
    if data.get("id") != native_id:
        raise _ProviderReadError("thread_scope_mismatch")
    own = {account.account_platform_id, account.webhook_target_id} - {""}
    kind, _reason, returned_peer = classify_conversation_identity(
        {"participants": data.get("participants")}, own_ids=own, sender_id=peer
    )
    if kind != "direct" or returned_peer != peer:
        raise _ProviderReadError("participants_unverified")
    return set(_identity_ids(data["participants"]))


def _native_api_base(account):
    from providers.facebook import BASE_URL
    from providers.instagram_login import API_BASE

    return BASE_URL if account.platform == "facebook" else API_BASE


def _request_native_thread(account, native_id, page_limit, after, peer):
    """One initial GET; at most two exact-thread GETs for an older page.

    Do not use BaseProvider._request: its 429 path logs bodies. The messages
    edge's cursor contract follows Meta's official SDK Cursor.load_next_page:
    github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/api.py
    Only `paging.cursors.after` is used, never the provider's `next` URL.
    """
    path = f"{_native_api_base(account)}/{quote(native_id, safe='')}"
    # Fresh default transports have no retries. No response URL is followed.
    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        if after is None:
            fields = "id,participants{id},messages.limit(" + str(page_limit) + "){" + _MESSAGE_FIELDS + "}"
            return _provider_json(client, account, path, {"fields": fields})
        data = _provider_json(client, account, path, {"fields": "id,participants{id}"})
        _verified_participants(data, account, native_id, peer)
        data["messages"] = _provider_json(
            client, account, path + "/messages", {"fields": _MESSAGE_FIELDS, "limit": page_limit, "after": after}
        )
        return data


def _valid_provider_cursor(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_PROVIDER_CURSOR_LENGTH
        and re.fullmatch(r"[A-Za-z0-9_+/=-]+", value) is not None
    )


def _supported_older_cursor(paging, account, native_id):
    """Validate the provider's route evidence without following/storing its URL.

    In particular, do not assume that Instagram Login supports the Facebook
    messages edge merely because it returned generic Graph pagination data.
    Any URL credential is discarded; only the matching opaque cursor survives.
    """
    after = paging.get("cursors", {}).get("after")
    next_url = paging.get("next")
    if not _valid_provider_cursor(after) or not isinstance(next_url, str) or not 0 < len(next_url) <= 8192:
        return None
    try:
        expected = urlsplit(f"{_native_api_base(account)}/{quote(native_id, safe='')}/messages")
        candidate = urlsplit(next_url)
        # IG Login can advertise v26.0 paging for a configured v25.0 request.
        # Match only that version pair and the exact remaining path; requests
        # still use _native_api_base and never follow the advertised URL.
        path_matches = candidate.path == expected.path or (
            account.platform == "instagram_login"
            and expected.path.startswith("/v25.0/")
            and candidate.path == "/v26.0/" + expected.path.removeprefix("/v25.0/")
        )
        if (
            candidate.scheme != "https"
            or candidate.netloc != expected.netloc
            or not path_matches
            or candidate.username is not None
            or candidate.password is not None
            or candidate.fragment
            or any(ord(char) <= 32 or ord(char) == 127 for char in next_url)
        ):
            return None
        query = parse_qsl(candidate.query, keep_blank_values=True, strict_parsing=True, max_num_fields=50)
        if [value for key, value in query if key == "after"] != [after]:
            return None
        if any(key in {"before", "offset", "since", "until"} for key, _value in query):
            return None
        return after
    except (ValueError, UnicodeError):
        return None


def _continuation_scope(identity, authorization, limit):
    context = getattr(authorization, "native_read_context", None)
    if context is None:
        return None
    # No platform/access credential or private anchor data enters the token.
    return salted_hmac(
        CONTINUATION_SALT, json.dumps([identity, context, limit], sort_keys=True, default=str)
    ).hexdigest()


def _parse_continuation(token, scope):
    try:
        if not scope or not isinstance(token, str) or not token or len(token) > MAX_CONTINUATION_LENGTH:
            raise ValueError
        value = signing.loads(token, salt=CONTINUATION_SALT, max_age=CONTINUATION_MAX_AGE)
        if not isinstance(value, dict) or not isinstance(value.get("scope"), str):
            raise ValueError
        kind = value.get("kind", "older_page")  # Accept still-valid legacy older-page tokens.
        if kind == "same_page":
            if (
                (value.get("after") is not None and not _valid_provider_cursor(value["after"]))
                or "after" not in value
                or type(value.get("page_count")) is not int
                or not 1 <= value["page_count"] <= MAX_ITEMS
                or type(value.get("offset")) is not int
                or not 0 < value["offset"] < value["page_count"]
                or not isinstance(value.get("page_digest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", value["page_digest"]) is None
            ):
                raise ValueError
        elif kind == "older_page":
            if not _valid_provider_cursor(value.get("after")) or _timestamp(value.get("oldest")) is None:
                raise ValueError
            previous_ids = value.get("previous_ids")
            if isinstance(previous_ids, str):
                packed = base64.b64decode(previous_ids, validate=True)
                if not 32 <= len(packed) <= 32 * MAX_ITEMS or len(packed) % 32:
                    raise ValueError
                previous_ids = [packed[index : index + 32].hex() for index in range(0, len(packed), 32)]
            if (
                not isinstance(previous_ids, list)
                or not 1 <= len(previous_ids) <= MAX_ITEMS
                or any(not isinstance(mid, str) or re.fullmatch(r"[0-9a-f]{64}", mid) is None for mid in previous_ids)
            ):
                raise ValueError
            value["previous_ids"] = previous_ids
        else:
            raise ValueError
        if value["scope"] != scope:
            raise NativeThreadReadError("stale_continuation", "The conversation or credential changed; reload it.")
        value["kind"] = kind
        return value
    except (signing.BadSignature, TypeError, ValueError, OverflowError):
        raise NativeThreadReadError(
            "invalid_continuation", "The older-history position expired or changed; reload it."
        ) from None


def _message_id_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _page_digest(items):
    identities = [(item["platform_message_id"], item["occurred_at"], item["direction"]) for item in items]
    return hashlib.sha256(json.dumps(identities, separators=(",", ":")).encode()).hexdigest()


def _encoded_size(value):
    # ASCII escapes bound both UTF-8 output and the nested JSON string used by MCP.
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def _fit_items(items):
    """Reserve every row's identity before distributing text/media space.

    Dropping even one valid row would make advancing a provider cursor lose
    that row forever. Reserve envelope/token space and truncate content only.
    """
    budget, kept = MAX_RESULT_BYTES - 8192, []
    bases = []
    for original in items:
        bare = {**original, "body": "", "attachments": [], "body_truncated": True, "attachments_truncated": True}
        bases.append(_encoded_size(bare) + 1)
    if sum(bases) > budget:
        raise _ProviderReadError("response_too_large")
    demands = [_encoded_size(original) + 1 - base for original, base in zip(items, bases, strict=True)]
    allowances = bases.copy()
    surplus = budget - sum(bases)
    # Satisfy short rows first so their unused shares reach longer rows. Rows
    # that still need truncation receive equal shares of the remaining space.
    for position, index in enumerate(sorted(range(len(items)), key=lambda index: demands[index])):
        allowance = min(demands[index], surplus // (len(items) - position))
        allowances[index] += allowance
        surplus -= allowance
    for original, remaining in zip(items, allowances, strict=True):
        item = deepcopy(original)
        while item["attachments"] and _encoded_size(item) + 1 > remaining:
            item["attachments"].pop()
            item["attachments_truncated"] = True
        if _encoded_size(item) + 1 > remaining and item["body"]:
            body = item["body"]
            item["body_truncated"] = True
            left, right = 0, len(body)
            while left < right:
                middle = (left + right + 1) // 2
                item["body"] = body[:middle]
                if _encoded_size(item) + 1 <= remaining:
                    left = middle
                else:
                    right = middle - 1
            item["body"] = body[:left]
        size = _encoded_size(item) + 1
        if size > remaining:
            raise _ProviderReadError("response_too_large")
        kept.append(item)
    return kept


def _result(message, account, limit, reason_code, *, items=None, scanned=0, skipped=0, more=False):
    original_items = items or []
    try:
        items = _fit_items(original_items)
    except _ProviderReadError:
        # No partially emitted page may acquire a continuation.
        reason_code, items = "response_too_large", []
    output_omitted = len(original_items) - len(items)
    output_truncated = items != original_items
    more = more or bool(output_omitted)
    body_truncated = sum(item["body_truncated"] for item in items)
    attachments_truncated = sum(item["attachments_truncated"] for item in items)
    return {
        "status": "observed" if reason_code in {"bounded_snapshot", "no_messages_observed"} else "unavailable",
        "reason_code": reason_code,
        "checked_at": timezone.now().isoformat(),
        "anchor_message_id": str(message.pk),
        "platform": account.platform,
        "items": items,
        "history_complete": False,
        "persisted": False,
        "coverage": {
            "kind": "one_time_native_thread",
            "requested_limit": limit,
            "provider_page_limit": min(limit, PROVIDER_PAGE_LIMIT),
            "scanned_count": scanned,
            "returned_count": len(items),
            "skipped_count": skipped,
            "truncated": bool(more or skipped or body_truncated or attachments_truncated),
            "body_truncated_count": body_truncated,
            "attachments_truncated_count": attachments_truncated,
            "output_byte_limit": MAX_RESULT_BYTES,
            "output_truncated": output_truncated,
            "output_omitted_count": output_omitted,
        },
        "more_available": more,
        "older_continuation": None,
        "older_history_status": "unavailable",
        "page_key": None,
        "newer_outbound_observed": any(
            item["direction"] == "outbound" and parse_datetime(item["occurred_at"]) > message.received_at
            for item in original_items
        ),
    }


def _timestamp(value):
    try:
        stamp = parse_datetime(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return (
        stamp
        if (
            stamp is not None
            and timezone.is_aware(stamp)
            and datetime(1970, 1, 1, tzinfo=UTC) < stamp <= timezone.now()
        )
        else None
    )


def _attachments_truncated(row):
    count = 0
    for key in ("attachments", "shares"):
        group = row.get(key, [])
        if isinstance(group, dict):
            paging = group.get("paging", {})
            if (
                not isinstance(paging, dict)
                or paging.get("next")
                or paging.get("previous")
                or group.get("has_more")
                or group.get("truncated")
                or group.get("is_truncated")
            ):
                return True
            total = group.get("summary", {})
            total = total.get("total_count") if isinstance(total, dict) else None
            group = group.get("data")
            if total is not None and (type(total) is not int or not isinstance(group, list) or total != len(group)):
                return True
        if not isinstance(group, list) or any(not isinstance(item, dict) for item in group):
            return True
        count += len(group)
    return count > MAX_ATTACHMENTS


def _project(data, account, message, native_id, peer, limit, *, scope=None, previous=None):
    def result(code, **kwargs):
        return _result(message, account, limit, code, **kwargs)

    try:
        participants = _verified_participants(data, account, native_id, peer)
    except _ProviderReadError as exc:
        return result(exc.code)
    own = {account.account_platform_id, account.webhook_target_id} - {""}
    page = data.get("messages")
    if not isinstance(page, dict) or not isinstance(page.get("data"), list):
        return result("invalid_response")
    rows = page["data"]
    # A provider can exceed the transport hint. Validate the whole bounded page
    # before exposing a slice; never advance past rows the caller has not seen.
    if len(rows) > MAX_ITEMS:
        return result("invalid_response", scanned=len(rows), skipped=len(rows), more=True)
    same_page = previous is not None and previous["kind"] == "same_page"
    older_page = previous if previous and not same_page else None
    pagination_supported = True
    paging = page.get("paging", {})
    if not isinstance(paging, dict):
        pagination_supported, paging = False, {}
    if any(key in paging and not isinstance(paging[key], str) for key in ("next", "previous")):
        pagination_supported = False
    cursors = paging.get("cursors", {})
    if not isinstance(cursors, dict):
        pagination_supported, cursors = False, {}
    more = bool(paging.get("next") or page.get("has_more") or page.get("truncated") or page.get("is_truncated"))
    summary = page.get("summary", {})
    if not isinstance(summary, dict):
        pagination_supported, summary = False, {}
    total = summary.get("total_count")
    if total is not None:
        if type(total) is not int or total < len(rows):
            pagination_supported = False
        more = more or total != len(rows)
    if previous and not pagination_supported:
        return result("invalid_response")
    if page.get("truncated") or page.get("is_truncated"):
        # A provider-reported omission cannot be repaired by advancing past it.
        if previous:
            return result("pagination_unavailable", scanned=len(rows), more=True)
        pagination_supported = False
    found, previous_stamp = {}, None
    for row in rows:
        if not isinstance(row, dict):
            return result("invalid_response", scanned=len(rows), skipped=1, more=more)
        sender = row.get("from")
        sender = native_thread_id(sender.get("id")) if isinstance(sender, dict) else ""
        recipients = _identity_ids(row.get("to"))
        if not sender or recipients is None or len(recipients) != 1 or {sender, recipients[0]} != participants:
            return result("message_scope_unverified")
        direction = "outbound" if sender in own else "inbound"
        if (direction == "inbound" and sender != peer) or (direction == "outbound" and recipients[0] != peer):
            return result("message_scope_unverified")
        mid, stamp = native_thread_id(row.get("id")), _timestamp(row.get("created_time"))
        body = row.get("message", "")
        if not mid or stamp is None or not isinstance(body, str):
            return result("invalid_response", scanned=len(rows), skipped=1, more=more)
        # Valid first-page observations remain useful even if the provider's
        # ordering cannot establish a safe older-page boundary. Older pages
        # must still preserve the expected ordering and direction of progress.
        if previous_stamp is not None and stamp > previous_stamp:
            if previous:
                return result("stale_page" if same_page else "pagination_unavailable", scanned=len(rows), more=True)
            pagination_supported = False
        previous_stamp = stamp
        if older_page and (
            stamp > _timestamp(older_page["oldest"]) or _message_id_digest(mid) in older_page["previous_ids"]
        ):
            return result("pagination_unavailable", scanned=len(rows), more=True)
        if is_deleted_content(row):
            body = ""
        item = {
            "platform_message_id": mid,
            "direction": direction,
            "body": body[:MAX_BODY_CHARACTERS],
            "occurred_at": stamp.astimezone(UTC).isoformat(),
            "attachments": normalize_attachments(row),
            "content_status": message_content_status(row, body),
            "source": "platform_observed",
            "body_truncated": len(body) > MAX_BODY_CHARACTERS,
            "attachments_truncated": _attachments_truncated(row),
        }
        if mid in found:
            return result("message_scope_unverified")
        found[mid] = item
    page_items = list(found.values())
    digest = _page_digest(page_items)
    if same_page and (len(page_items) != previous["page_count"] or digest != previous["page_digest"]):
        return result("stale_page", scanned=len(rows), more=True)
    offset = previous["offset"] if same_page else 0
    page_size = min(limit, PROVIDER_PAGE_LIMIT)
    end = min(offset + page_size, len(page_items))
    items = sorted(page_items[offset:end], key=lambda item: (item["occurred_at"], item["platform_message_id"]))
    if previous is None and not pagination_supported:
        # An unordered first page can still show the newest validated rows,
        # but its provider positions are never safe continuation offsets.
        items = sorted(page_items, key=lambda item: (item["occurred_at"], item["platform_message_id"]))[-page_size:]
    unread = end < len(page_items)
    after = cursors.get("after")
    if previous and previous["after"] is not None and paging.get("next") and after == previous["after"]:
        return result("pagination_unavailable", scanned=len(rows), more=True)
    output = result(
        "bounded_snapshot" if items else "no_messages_observed",
        items=items,
        scanned=len(rows),
        more=more or unread,
    )
    if output["status"] != "observed":
        return output
    output["page_key"] = hashlib.sha256(json.dumps([scope, digest, offset, end]).encode()).hexdigest()
    output["older_history_status"] = "unavailable" if more or unread or not pagination_supported else "not_indicated"
    after = _supported_older_cursor(paging, account, native_id) if pagination_supported else None
    position = None
    if items and scope and pagination_supported:
        if unread:
            position = {
                "scope": scope,
                "kind": "same_page",
                "after": previous["after"] if previous else None,
                "offset": end,
                "page_count": len(page_items),
                "page_digest": digest,
            }
        elif after:
            # Pack full SHA-256 digests so even 100 IDs and a maximal provider
            # cursor fit a bounded signed token without including raw IDs.
            packed = b"".join(bytes.fromhex(_message_id_digest(item["platform_message_id"])) for item in page_items)
            position = {
                "scope": scope,
                "kind": "older_page",
                "after": after,
                "oldest": page_items[-1]["occurred_at"],
                "previous_ids": base64.b64encode(packed).decode("ascii"),
            }
    if position:
        continuation = signing.dumps(position, salt=CONTINUATION_SALT, compress=True)
        candidate = {**output, "older_continuation": continuation, "older_history_status": "available"}
        if len(continuation) <= MAX_CONTINUATION_LENGTH and len(json.dumps(candidate)) <= MAX_RESULT_BYTES:
            output = candidate
    return output


def read_native_thread(message, *, authorization, limit=50, continuation=None):
    """Read only this stored anchor's known native thread; never grant sending."""
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise NativeThreadReadError("invalid_limit", "The snapshot limit must be an integer between 1 and 100.")
    with transaction.atomic():
        account, current = _selected(message, authorization)
        canonical, canonical_identity = _canonical(current)
        before = _identity(account, current, canonical_identity)
        if account.platform not in {"facebook", "instagram_login"}:
            return _result(current, account, limit, "unsupported_platform")
        if (
            account.connection_status != "connected"
            or not account.oauth_access_token
            or (account.token_expires_at is not None and account.token_expires_at <= timezone.now())
        ):
            return _result(current, account, limit, "account_unavailable")
        native_id = thread_id(current)
        if not native_id or native_id in {".", ".."} or any(char in native_id for char in "/\\?#%"):
            return _result(current, account, limit, "missing_native_thread")
        peer = _peer(account, current, canonical)
        if not peer:
            return _result(current, account, limit, "unverified_thread")
        sibling_identity, sibling_conflict = _sibling_evidence(account, current, peer)
        if sibling_conflict:
            return _result(current, account, limit, "unverified_thread")
        scope = _continuation_scope(before, authorization, limit)
        previous = _parse_continuation(continuation, scope) if continuation is not None else None
    data, failure = None, None
    try:
        data = _request_native_thread(
            account, native_id, min(limit, PROVIDER_PAGE_LIMIT), previous["after"] if previous else None, peer
        )
    except _ProviderReadError as exc:
        failure = exc.code
    except Exception:
        # Never reflect or log provider bodies, URLs, credentials or diagnostics.
        failure = "provider_unavailable"
    with transaction.atomic():
        account, current = _selected(message, authorization)
        _row, canonical_identity = _canonical(current)
        if _identity(account, current, canonical_identity) != before:
            raise NativeThreadReadError("stale", "The conversation or account changed during the check; reload it.")
        current_siblings, sibling_conflict = _sibling_evidence(account, current, peer)
        if current_siblings != sibling_identity or sibling_conflict:
            raise NativeThreadReadError("stale", "The conversation identity changed during the check; reload it.")
        if account.token_expires_at is not None and account.token_expires_at <= timezone.now():
            return _result(current, account, limit, "account_unavailable")
        if failure:
            return _result(current, account, limit, failure)
        return _project(data, account, current, native_id, peer, limit, scope=scope, previous=previous)
