"""Bounded normalized contracts shared by pages and verified webhook receipts."""

import calendar
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from django.utils import timezone

from .sync_identity import SyncError

MAX_ITEMS = 100
MAX_BYTES = 1024 * 1024
CONTEXTS = {"bootstrap", "backfill", "live", "repair"}


def valid_id(value):
    return isinstance(value, str) and 0 < len(value) <= 255 and not any(c.isspace() or ord(c) < 32 for c in value)


def valid_cursor(value):
    return (
        isinstance(value, str)
        and len(value) <= 1024
        and (not value or re.fullmatch(r"[A-Za-z0-9_+/=-]+", value) is not None)
    )


def valid_time(value, now):
    return isinstance(value, datetime) and timezone.is_aware(value) and value.year > 1970 and value <= now


def calendar_months(value, months):
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    return value.replace(year=year, month=month + 1, day=min(value.day, calendar.monthrange(year, month + 1)[1]))


def content_fingerprint(body, attachments):
    return hashlib.sha256(json.dumps([body, attachments], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class MessageObservation:
    platform_message_id: str
    conversation_id: str
    sender_id: str
    recipient_id: str
    participant_ids: tuple[str, ...]
    body: str
    occurred_at: datetime | None
    observed_at: datetime
    attachments: tuple = ()
    sender_name: str = ""
    source: str = "poll"
    provider_updated_at: datetime | None = None
    provider_revision: int | None = None
    snapshot_started_at: datetime | None = None
    withdrawn_verified: bool = False
    content_available: bool = True
    outbound_verified: bool = False
    conversation_type: str = ""
    classification_reason: str = ""
    content_fetch_status: str = ""
    attachments_complete: bool = True


@dataclass(frozen=True)
class ConversationObservation:
    platform_conversation_id: str
    participant_ids: tuple[str, ...]


@dataclass(frozen=True)
class SyncPage:
    observations: tuple = ()
    next_cursor: str = ""
    exhausted: bool = True
    observed_at: datetime = field(default_factory=timezone.now)


def validate_observation(item, *, now, allow_unattributed_withdrawal=False, allow_pending_identity=False):
    if not isinstance(item, MessageObservation) or not valid_id(item.platform_message_id):
        raise SyncError("invalid_observation")
    unattributed = (
        allow_pending_identity or (allow_unattributed_withdrawal and item.withdrawn_verified)
    ) and not item.conversation_id
    if not unattributed and (not valid_id(item.conversation_id) or not valid_id(item.sender_id)):
        raise SyncError("invalid_observation")
    if (
        (item.sender_id and not valid_id(item.sender_id))
        or (item.recipient_id and not valid_id(item.recipient_id))
        or not isinstance(item.participant_ids, tuple)
        or len(item.participant_ids) > 100
        or any(not valid_id(value) for value in item.participant_ids)
        or len(set(item.participant_ids)) != len(item.participant_ids)
        or not isinstance(item.body, str)
        or len(item.body) > 10000
        or not isinstance(item.sender_name, str)
        or len(item.sender_name) > 255
        or not isinstance(item.attachments, tuple)
        or len(item.attachments) > 20
        or not isinstance(item.source, str)
        or item.source not in {"poll", "webhook", "app_send", "legacy_backfill"}
        or not isinstance(item.conversation_type, str)
        or item.conversation_type not in {"", "direct", "group", "unknown"}
        or not isinstance(item.classification_reason, str)
        or item.classification_reason
        not in {
            "",
            "participants_pair",
            "participants_group",
            "participants_missing",
            "participants_invalid",
            "participants_incomplete",
            "participant_endpoints_conflict",
            "identity_conflict",
        }
        or not isinstance(item.content_fetch_status, str)
        or item.content_fetch_status not in {"", "fields_requested", "basic_fallback"}
        or not valid_time(item.observed_at, now)
        or (item.occurred_at is not None and not valid_time(item.occurred_at, now))
        or (item.provider_updated_at is not None and not valid_time(item.provider_updated_at, now))
        or (
            item.snapshot_started_at is not None
            and (not valid_time(item.snapshot_started_at, now) or item.snapshot_started_at > item.observed_at)
        )
        or (
            item.provider_revision is not None
            and (
                isinstance(item.provider_revision, bool)
                or not isinstance(item.provider_revision, int)
                or not 0 <= item.provider_revision < 2**63
            )
        )
        or any(
            not isinstance(value, bool)
            for value in (
                item.withdrawn_verified,
                item.content_available,
                item.outbound_verified,
                item.attachments_complete,
            )
        )
    ):
        raise SyncError("invalid_observation")


def validate_page(page, lease, now):
    if (
        not isinstance(page, SyncPage)
        or not isinstance(page.observations, tuple)
        or len(page.observations) > MAX_ITEMS
        or not valid_cursor(page.next_cursor)
        or not isinstance(page.exhausted, bool)
        or page.exhausted != (page.next_cursor == "")
        or not valid_time(page.observed_at, now)
        or page.observed_at < lease.claimed_at
    ):
        raise SyncError("invalid_page")
    seen = set()
    for item in page.observations:
        if lease.stream == "conversations":
            if (
                not isinstance(item, ConversationObservation)
                or not valid_id(item.platform_conversation_id)
                or not isinstance(item.participant_ids, tuple)
                or len(item.participant_ids) > 100
                or len(set(item.participant_ids)) != len(item.participant_ids)
                or any(not valid_id(value) for value in item.participant_ids)
            ):
                raise SyncError("invalid_page_identity")
            native_id = item.platform_conversation_id
        else:
            validate_observation(item, now=now)
            if (
                item.conversation_id != lease.scope_key
                or item.observed_at < lease.claimed_at
                or (item.snapshot_started_at and item.snapshot_started_at < lease.claimed_at)
            ):
                raise SyncError("invalid_page_identity")
            native_id = item.platform_message_id
        if native_id in seen:
            raise SyncError("duplicate_page_identity")
        seen.add(native_id)
    try:
        encoded = json.dumps(asdict(page), sort_keys=True, default=str).encode()
    except (ValueError, TypeError, RecursionError):
        raise SyncError("invalid_page") from None
    if len(encoded) > MAX_BYTES:
        raise SyncError("page_too_large")
    return hashlib.sha256(encoded).hexdigest()
