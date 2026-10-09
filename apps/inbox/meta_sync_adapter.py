"""Pinned Meta GETs; provider next URLs are evidence, never a request target."""

import json
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.social_accounts.models import SocialAccount
from providers.analytics_errors import _numeric_code
from providers.meta_inbox_content import (
    BASIC_MESSAGE_FIELDS,
    CONTENT_MESSAGE_FIELDS,
    _identity_ids,
    normalize_attachments,
    polled_message_extra,
)
from providers.meta_inbox_paging import PAGE_LIMITS, is_page_size_rejection

from .durable_sync import ROUTE_CONTRACT, capture_permitted
from .models import InboxSyncCheckpoint, InboxSyncConnection
from .sender_display import normalize_sender_name
from .sync_contracts import (
    MAX_BYTES,
    MAX_ITEMS,
    ConversationObservation,
    MessageObservation,
    SyncPage,
    valid_cursor,
    valid_id,
)
from .sync_identity import SyncError


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _retry_after(value):
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        return max(1, int(value))
    except ValueError:
        try:
            return max(1, int((parsedate_to_datetime(value) - timezone.now()).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None


def _unsupported_optional_field(error):
    message = error.get("message")
    if not isinstance(message, str) or len(message) > 2000:
        return False
    return bool(
        re.search(
            r"\b(?:nonexisting|non-existing|unknown|unsupported)\s+field\s*\(?[\"']?(?:attachments|shares|image_data|video_data|file_url|name|type|url)\b",
            message,
            re.IGNORECASE,
        )
    )


def _get(client, account, route, params, *, edge=True, optional_content=False):
    deadline = time.monotonic() + 20.0
    status_code = None
    try:
        with client.stream(
            "GET",
            route,
            params=params,
            headers={"Authorization": f"Bearer {account.oauth_access_token}", "Accept-Encoding": "identity"},
        ) as response:
            status_code = response.status_code
            if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise SyncError("invalid_response")
            if response.status_code in {401, 403}:
                raise SyncError("permission_unavailable")
            if response.status_code == 429:
                raise SyncError("rate_limited", retry_after=_retry_after(response.headers.get("Retry-After")))
            if response.status_code not in {400, 500} and not 200 <= response.status_code < 300:
                raise SyncError("provider_unavailable")
            content = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise SyncError("provider_unavailable")
                if len(content) + len(chunk) > MAX_BYTES:
                    # Oversized error pages are still transient failures, not
                    # evidence that a successful provider page is malformed.
                    raise SyncError("provider_unavailable" if status_code == 500 else "response_too_large")
                content.extend(chunk)
        data = json.loads(content, object_pairs_hook=_unique_object)
    except SyncError:
        raise
    except httpx.HTTPError:
        raise SyncError("provider_unavailable") from None
    except (ValueError, UnicodeError, RecursionError):
        if status_code == 500:
            raise SyncError("provider_unavailable") from None
        raise SyncError("invalid_response") from None
    if response.status_code in {400, 500}:
        error = data.get("error") if isinstance(data, dict) else None
        error = error if isinstance(error, dict) else {}
        if edge and params.get("limit") and is_page_size_rejection(response.status_code, error):
            # The next fenced, budgeted attempt retries this exact cursor with
            # fewer rows. No extra GET or reduced content/identity fields here.
            raise SyncError("page_size_rejected")
        if response.status_code == 500:
            raise SyncError("provider_unavailable")
        code = _numeric_code(error.get("code"))
        if code in {10, 102, 190, 200}:
            raise SyncError("permission_unavailable")
        if code in {4, 17, 32, 341, 613}:
            raise SyncError("rate_limited", retry_after=_retry_after(response.headers.get("Retry-After")))
        if code in {1, 2} or error.get("is_transient") is True:
            raise SyncError("provider_unavailable")
        if code == 100 and optional_content and _unsupported_optional_field(error):
            raise SyncError("content_fields_unavailable")
        details = error.get("error_data")
        if (
            code == 100
            and params.get("after")
            and isinstance(details, dict)
            and details.get("blame_field_specs") == [["after"]]
        ):
            raise SyncError("cursor_invalid")
        raise SyncError("invalid_response")
    if not isinstance(data, dict) or (
        edge and (not isinstance(data.get("data"), list) or len(data["data"]) > MAX_ITEMS)
    ):
        raise SyncError("invalid_response")
    return data


def _cursor(data, route, platform, stream, params):
    paging = data.get("paging", {})
    if not isinstance(paging, dict):
        raise SyncError("invalid_response")
    if not paging.get("next"):
        return ""
    after = paging.get("cursors", {}).get("after") if isinstance(paging.get("cursors"), dict) else None
    next_url = paging["next"]
    if not after or not valid_cursor(after) or not isinstance(next_url, str) or len(next_url) > 8192:
        raise SyncError("pagination_unverified")
    try:
        expected, candidate = urlsplit(route), urlsplit(next_url)
        path_matches = candidate.path == expected.path or (
            platform == "instagram_login"
            and stream == "messages"
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
            raise ValueError
        query = parse_qsl(candidate.query, keep_blank_values=True, strict_parsing=True, max_num_fields=50)
        if [value for key, value in query if key == "after"] != [after] or any(
            key in {"before", "offset", "since", "until"} for key, _ in query
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        raise SyncError("pagination_unverified") from None
    if (
        platform == "instagram_login"
        and stream == "messages"
        and not data["data"]
        and params.get("after") == after
        and len(query) == len(params) == 3
        and dict(query) == {key: str(value) for key, value in params.items()}
        and set(params) == {"after", "fields", "limit"}
    ):
        # An empty self-cursor is not EOF. Only the exact verified request
        # shape qualifies for the separately preserved live-head refresh path.
        raise SyncError("pagination_no_progress")
    return after


def _current_account(lease):
    account = SocialAccount.objects.filter(pk=lease.account_id, workspace_id=lease.workspace_id).first()
    connection = InboxSyncConnection.objects.filter(pk=lease.connection_id).first()
    if (
        connection is None
        or not capture_permitted(account, connection)
        or connection.generation != lease.connection_generation
        or connection.auth_fingerprint != lease.auth_fingerprint
        or lease.route_contract != ROUTE_CONTRACT
    ):
        raise SyncError("enrollment_or_identity_revoked")
    if not InboxSyncCheckpoint.objects.filter(
        pk=lease.checkpoint_id,
        connection_id=lease.connection_id,
        connection_generation=lease.connection_generation,
        scan_generation=lease.scan_generation,
        fence=lease.fence,
        lease_token=lease.token,
        lease_expires_at__gt=timezone.now(),
        cursor=lease.cursor,
        stream=lease.stream,
        scope_key=lease.scope_key,
        context=lease.context,
        content_fields_mode=lease.content_fields_mode,
        attempts=lease.attempts,
    ).exists():
        raise SyncError("lease_lost")
    return account


class MetaSyncAdapter:
    def __init__(self, *, transport=None):
        self.transport = transport

    @staticmethod
    def required_gets(stream):
        return 2 if stream == "messages" else 1

    def fetch(self, lease):
        account = _current_account(lease)
        base = (
            "https://graph.facebook.com/v25.0"
            if account.platform == "facebook"
            else "https://graph.instagram.com/v25.0"
        )
        # Reuse the durable per-page retry counter rather than add a schema or
        # spend unreserved GETs. Other transient retries are also safely smaller;
        # a successful commit resets the counter for the next provider page.
        page_limit = PAGE_LIMITS[min(lease.attempts, len(PAGE_LIMITS) - 1)]
        if lease.stream == "conversations":
            route = f"{base}/{quote(account.account_platform_id, safe='')}/conversations"
            params = {"fields": "id,participants{id}", "limit": page_limit}
            if account.platform == "instagram_login":
                params["platform"] = "instagram"
        else:
            route = f"{base}/{quote(lease.scope_key, safe='')}/messages"
            fields = BASIC_MESSAGE_FIELDS if lease.content_fields_mode == "basic" else CONTENT_MESSAGE_FIELDS
            params = {"fields": fields + ",to{id}", "limit": page_limit}
        if lease.cursor:
            params["after"] = lease.cursor
        started = timezone.now()
        participants = lease.participants
        with httpx.Client(timeout=20.0, follow_redirects=False, transport=self.transport) as client:
            if lease.stream == "messages":
                metadata = _get(
                    client, account, route.removesuffix("/messages"), {"fields": "id,participants{id}"}, edge=False
                )
                if metadata.get("id") != lease.scope_key:
                    raise SyncError("invalid_page_identity")
                participants = tuple(_identity_ids(metadata.get("participants")) or ())
                _current_account(lease)  # Revocation stops the second HTTP call too.
            data = _get(
                client,
                account,
                route,
                params,
                optional_content=(lease.stream == "messages" and lease.content_fields_mode == "extended"),
            )
        observed = timezone.now()
        cursor = _cursor(data, route, account.platform, lease.stream, params)
        items = []
        for item in data["data"]:
            if not isinstance(item, dict) or not valid_id(item.get("id")):
                raise SyncError("invalid_page_identity")
            if lease.stream == "conversations":
                items.append(ConversationObservation(item["id"], tuple(_identity_ids(item.get("participants")) or ())))
                continue
            sender = item.get("from")
            if not isinstance(sender, dict) or not valid_id(sender.get("id")):
                raise SyncError("invalid_observation")
            stamp = parse_datetime(item["created_time"]) if isinstance(item.get("created_time"), str) else None
            if stamp is None or timezone.is_naive(stamp) or stamp.year <= 1970 or stamp > observed:
                # Missing/unverified time is undated saved history, not server
                # receipt chronology or permission to open a reply window.
                stamp = None
            extra = polled_message_extra(
                item,
                conversation_id=lease.scope_key,
                sender_id=sender["id"],
                own_id=account.account_platform_id,
                participant_ids=list(participants),
                content_fetch_status="fields_requested",
            )
            items.append(
                MessageObservation(
                    platform_message_id=item["id"],
                    conversation_id=lease.scope_key,
                    sender_id=sender["id"],
                    recipient_id=extra.get("message_recipient_id", ""),
                    participant_ids=tuple(extra.get("participant_ids", ())),
                    body=item.get("message", ""),
                    sender_name=normalize_sender_name(sender),
                    occurred_at=stamp,
                    observed_at=observed,
                    snapshot_started_at=started,
                    attachments=tuple(normalize_attachments(extra)),
                    conversation_type=extra.get("conversation_type", ""),
                    classification_reason=extra.get("classification_reason", ""),
                    content_fetch_status="basic_fallback"
                    if lease.content_fields_mode == "basic"
                    else "fields_requested",
                    attachments_complete=lease.content_fields_mode != "basic",
                    content_available=item.get("is_deleted") is not True
                    and item.get("is_unsupported") is not True
                    and any(key in item for key in ("message", "attachments", "shares", "story")),
                    # Poll metadata is not the signed Instagram deletion capability.
                    withdrawn_verified=False,
                )
            )
        return SyncPage(tuple(items), cursor, not cursor, observed)
