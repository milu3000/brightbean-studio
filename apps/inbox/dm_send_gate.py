"""Persisted, account-scoped barrier for BrightBean DM sends only.

Enrollment is a server-side operation, never implied by a feature flag. The
committed unknown attempt is deliberately conservative: it can mean a process
stopped before HTTP. Only the invocation that created it can settle it here.
There is no recovery-by-timeout, reconciliation, or public mutation endpoint.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount

from .locking import lock_dm_account
from .models import DMSendAttempt, DMSendControl, InboxMessage, InboxReply
from .services import ReplyStateError

COVERAGE_VERSION = "brightbean-dm-gate-v1"
SUPPORTED_PLATFORMS = {"facebook", "instagram_login"}
UNKNOWN_MESSAGE = "Delivery outcome is unknown. DM sends for this account are held; do not retry."
Authorization = Callable[[SocialAccount], None]


@dataclass(frozen=True)
class SendSnapshot:
    account_id: object
    workspace_id: object
    platform: str
    native_id: str
    fingerprint: str


def capture_send_snapshot(reply):
    """Pin the caller's already-scoped target before any waiting or refresh."""
    message = reply.inbox_message
    if message.message_type != InboxMessage.MessageType.DM:
        return None
    account = message.social_account
    return SendSnapshot(
        account.pk, message.workspace_id, account.platform, account.account_platform_id, _fingerprint(reply, message)
    )


def check_snapshot_identity(snapshot, account):
    if snapshot is None or (
        snapshot.account_id != account.pk
        or snapshot.workspace_id != account.workspace_id
        or snapshot.platform != account.platform
        or snapshot.native_id != account.account_platform_id
    ):
        raise DMSendGateError("target_changed", "The originally selected DM account changed; reload before sending.")


class DMSendGateError(ReplyStateError):
    """A stable, non-provider diagnostic safe for the public send surfaces."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class DMSendUnknownError(DMSendGateError):
    def __init__(self):
        super().__init__("outcome_unknown", UNKNOWN_MESSAGE)


class EnrolledDMSendError(Exception):
    """Internal redirect raised before legacy dispatch, under the account lock."""


def _outermost_required():
    # durable=True alone permits Django TestCase's wrapper. Do not allow even
    # that exception here: a savepoint is not a durable pre-network commit.
    if connection.in_atomic_block or not connection.get_autocommit():
        raise DMSendGateError("outer_transaction", "DM sending requires an outermost committed transaction boundary.")


def _identity_matches(control, account):
    return (
        control.workspace_id == account.workspace_id
        and control.platform == account.platform
        and control.account_platform_id == account.account_platform_id
    )


def _check_identity(control, account):
    if not _identity_matches(control, account):
        raise DMSendGateError("identity_changed", "The enrolled DM account identity changed; sending is held.")
    if control.platform not in SUPPORTED_PLATFORMS or control.coverage_version != COVERAGE_VERSION:
        raise DMSendGateError("coverage_unsupported", "This DM send coverage is unsupported; sending is held.")


def _authorize(authorization, account):
    if authorization is None:
        raise DMSendGateError("authorization_required", "Current DM send authorization is required.")
    authorization(account)


def session_send_authorization(user) -> Authorization:
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
            not member
            or (member.custom_role_id and member.custom_role.organization_id != member.workspace.organization_id)
            or not member.effective_permissions.get("reply_from_inbox", False)
        ):
            raise DMSendGateError("authorization_revoked", "Current permission to send this DM is unavailable.")

    return authorize


def key_send_authorization(api_key, request=None) -> Authorization:
    """Recheck current key grants or the original OAuth bearer at dispatch.

    The bearer stays in the request closure only; it is never stored in gate
    metadata. Cached/discovered tool permissions do not authorize a send.
    """
    workspace_id, actor_id = api_key.workspace_id, api_key.issued_by_id

    def authorize(account):
        from apps.api.auth import _resolve_oauth_actor
        from apps.api_keys.models import ApiKey

        if account.workspace_id != workspace_id:
            raise DMSendGateError("authorization_revoked", "Current permission to send this DM is unavailable.")
        if getattr(api_key, "is_oauth", False):
            header = request.headers.get("Authorization", "") if request is not None else ""
            current = _resolve_oauth_actor(header[7:]) if header.startswith("Bearer ") else None
            permitted = (
                current is not None
                and current.workspace_id == workspace_id
                and current.issued_by_id == actor_id
                and current.effective_permissions.get("reply_from_inbox", False)
            )
        else:
            key = ApiKey.objects.filter(pk=api_key.pk, workspace_id=workspace_id, issued_by_id=actor_id).first()
            permitted = (
                key is not None
                and key.is_active
                and "reply_from_inbox" in (key.permissions or [])
                and key.social_accounts.filter(pk=account.pk, workspace_id=workspace_id).exists()
            )
        if not permitted:
            raise DMSendGateError("authorization_revoked", "Current permission to send this DM is unavailable.")
        session_send_authorization(api_key.issued_by)(account)

    return authorize


def enroll_dm_send_control(*, account_id, workspace_id, platform, account_platform_id) -> DMSendControl:
    """Explicit operator enrollment, initially paused, no historic certainty.

    Caller must already have authority to enroll this exact identity. This is
    deliberately not exposed to API/MCP/UI and does not alter their grants.
    """
    _outermost_required()
    with transaction.atomic(durable=True):
        account = lock_dm_account(account_id, workspace_id)
        if account is None or account.platform != platform or account.account_platform_id != account_platform_id:
            raise DMSendGateError("identity_changed", "The requested DM account identity does not match.")
        if platform not in SUPPORTED_PLATFORMS:
            raise DMSendGateError("coverage_unsupported", "This platform does not support the DM send gate.")
        control, _ = DMSendControl.objects.get_or_create(
            social_account=account,
            defaults={
                "workspace_id": workspace_id,
                "platform": platform,
                "account_platform_id": account_platform_id,
                "coverage_from": timezone.now(),
                "coverage_version": COVERAGE_VERSION,
            },
        )
        _check_identity(control, account)
    return control


def set_dm_send_paused(*, account_id, workspace_id, paused: bool, expected_epoch: int) -> DMSendControl:
    """Server-only control contract. Return is an acknowledgement of commit.

    Lock acquisition timeout/failure raises, never acknowledges a pause. Resume
    fences every old draft AND old target, including ordinary unanswered DMs.
    It does not clear attempts or recall messages already accepted by Meta.
    """
    _outermost_required()
    with transaction.atomic(durable=True):
        account = lock_dm_account(account_id, workspace_id)
        control = DMSendControl.objects.filter(social_account_id=account_id).first()
        if account is None or control is None:
            raise DMSendGateError("not_enrolled", "This DM account is not enrolled in the requested workspace.")
        _check_identity(control, account)
        if control.epoch != expected_epoch:
            raise DMSendGateError("stale_epoch", "The DM send control changed; reload its state.")
        if control.paused != paused:
            control.paused = paused
            control.epoch += 1
            if not paused:
                control.resume_cutoff = timezone.now()
            control.save(update_fields=["paused", "epoch", "resume_cutoff", "updated_at"])
    return control


def dm_send_status(account) -> dict:
    # One SQL snapshot: don't combine a pre-resume epoch with a post-resume
    # attempt count. Metadata reads need not hold the dispatch account lock.
    control = (
        DMSendControl.objects.select_related("social_account")
        .annotate(tracked_unresolved=Count("attempts", filter=Q(attempts__outcome=DMSendAttempt.Outcome.UNKNOWN)))
        .filter(social_account_id=account.pk)
        .first()
    )
    if control is None:
        return {"enrolled": False, "enforcement_scope": "BrightBean DM", "legacy_coverage_incomplete": True}
    if control.social_account.workspace_id != account.workspace_id:
        raise DMSendGateError("identity_changed", "The DM account identity changed; reload its state.")
    _check_identity(control, control.social_account)
    return {
        "enrolled": True,
        "enforcement_scope": "BrightBean DM",
        "pause_committed": control.paused,
        "epoch": control.epoch,
        "tracked_unresolved": control.tracked_unresolved,
        "observed_at": timezone.now(),
        "coverage_from": control.coverage_from,
        "coverage_version": control.coverage_version,
        "resume_cutoff": control.resume_cutoff,
        "identity_matches": True,
        "legacy_coverage_incomplete": True,
        "external_consumer_queue": "unobservable",
        "drafts_are_queued_sends": False,
    }


def _valid_time(value, cutoff, now):
    return isinstance(value, datetime) and timezone.is_aware(value) and cutoff < value <= now


def _fingerprint(reply, message):
    # No message text, recipients, platform payloads or secrets in this table.
    payload = [
        str(message.pk),
        str(message.workspace_id),
        str(message.social_account_id),
        message.message_type,
        message.platform_message_id,
        message.sender_handle,
        message.extra,
        message.received_at.isoformat(),
        message.created_at.isoformat(),
        reply.body,
        reply.created_at.isoformat(),
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _check_send(control, account, reply, message, authorization, automated):
    from .services import validate_automated_reply_window

    _check_identity(control, account)
    if control.paused:
        raise DMSendGateError("paused", "BrightBean DM sending is paused for this account.")
    if (
        message.message_type != InboxMessage.MessageType.DM
        or message.social_account_id != account.pk
        or message.workspace_id != account.workspace_id
    ):
        raise DMSendGateError("target_changed", "The DM target identity changed; sending is held.")
    cutoff = control.resume_cutoff or control.coverage_from
    now = timezone.now()
    if not all(
        _valid_time(value, cutoff, now) for value in (message.received_at, message.created_at, reply.created_at)
    ):
        raise DMSendGateError(
            "old_target", "This DM target or draft predates resume, or has an invalid timestamp; sending is held."
        )
    if not reply.body.strip():
        raise DMSendGateError("empty_body", "The DM reply body is empty.")
    if account.connection_status != "connected":
        raise DMSendGateError("disconnected", "The DM account is not connected.")
    _authorize(authorization, account)
    if automated:
        validate_automated_reply_window(message)


def _lock_target(reply, account):
    from .services import _lock_reply

    _lock_reply(reply)
    message = InboxMessage.objects.select_for_update().get(pk=reply.inbox_message_id)
    if message.social_account_id != account.pk or message.workspace_id != account.workspace_id:
        raise DMSendGateError("target_changed", "The DM target identity changed; sending is held.")
    message.social_account = account
    return message


def _prepare_attempt(reply, authorization, automated, snapshot):
    _outermost_required()
    with transaction.atomic(durable=True):
        identity = (
            InboxReply.objects.filter(pk=reply.pk)
            .values("inbox_message__social_account_id", "inbox_message__workspace_id")
            .first()
        )
        if identity is None:
            raise DMSendGateError("discarded", "This reply has been discarded.")
        account = lock_dm_account(identity["inbox_message__social_account_id"], identity["inbox_message__workspace_id"])
        if account is None:
            raise DMSendGateError("identity_changed", "The DM account identity changed.")
        check_snapshot_identity(snapshot, account)
        control = DMSendControl.objects.get(social_account=account)
        if control.attempts.filter(outcome=DMSendAttempt.Outcome.UNKNOWN).exists():
            raise DMSendUnknownError()
        message = _lock_target(reply, account)
        if reply.status not in {InboxReply.Status.DRAFT, InboxReply.Status.FAILED}:
            raise DMSendGateError("reply_state", "This reply cannot be sent again.")
        _check_send(control, account, reply, message, authorization, automated)
        if _fingerprint(reply, message) != snapshot.fingerprint:
            raise DMSendGateError(
                "target_changed", "The originally selected DM target or draft changed; reload before sending."
            )
        attempt = DMSendAttempt.objects.create(
            control=control, reply=reply, epoch=control.epoch, fingerprint=_fingerprint(reply, message)
        )
        reply.status, reply.send_error = InboxReply.Status.UNKNOWN, UNKNOWN_MESSAGE
        reply.save(update_fields=["status", "send_error", "updated_at"])
    return attempt


def _known_refusal(exc, platform):
    # The supported adapters make one POST. Only explicit authenticated/scope
    # refusals are known here. Generic ProviderError, 408/5xx, parse errors,
    # NotImplementedError and status-less rate limits remain unknown.
    from providers.exceptions import APIError, TokenExpiredError

    provider_name = {"facebook": "Facebook", "instagram_login": "Instagram (Direct)"}.get(platform)
    return (
        isinstance(exc, APIError | TokenExpiredError)
        and exc.platform == provider_name
        and exc.status_code in {401, 403}
    )


def _settle_not_sent(reply, attempt, code):
    attempt.outcome, attempt.reason_code = DMSendAttempt.Outcome.NOT_SENT, code
    attempt.completed_at = timezone.now()
    attempt.save(update_fields=["outcome", "reason_code", "completed_at"])
    reply.status = InboxReply.Status.FAILED
    reply.send_error = f"DM send stopped ({code}); no provider acceptance was recorded for this attempt."
    reply.save(update_fields=["status", "send_error", "updated_at"])


def send_enrolled_dm(reply, *, actor, authorization, automated, snapshot):
    from .services import _apply_post_send_side_effects, _dispatch_to_platform

    attempt = _prepare_attempt(reply, authorization, automated, snapshot)
    failure = None
    try:
        with transaction.atomic(durable=True):
            account = lock_dm_account(attempt.control.social_account_id, attempt.control.workspace_id)
            if account is None:
                raise DMSendGateError("identity_changed", "The DM account identity changed.")
            control = DMSendControl.objects.get(pk=attempt.control_id)
            message = _lock_target(reply, account)
            attempt.refresh_from_db()
            try:
                if attempt.outcome != DMSendAttempt.Outcome.UNKNOWN or reply.status != InboxReply.Status.UNKNOWN:
                    raise DMSendGateError("attempt_changed", "The durable DM attempt changed; sending is held.")
                _check_send(control, account, reply, message, authorization, automated)
                if control.epoch != attempt.epoch or _fingerprint(reply, message) != attempt.fingerprint:
                    raise DMSendGateError("stale_attempt", "The DM target, draft or gate changed; sending is held.")
            except (ReplyStateError, ValueError) as exc:
                # This invocation has not entered dispatch. A new invocation
                # cannot reach this branch because _prepare_attempt rejects it.
                _settle_not_sent(reply, attempt, getattr(exc, "code", "preflight_refused"))
                failure = exc
            if failure is None:
                try:
                    mid = _dispatch_to_platform(message, reply.body, automated=automated)
                    if not isinstance(mid, str) or not mid.strip() or len(mid) > 255:
                        raise DMSendUnknownError()
                except Exception as exc:
                    if _known_refusal(exc, account.platform):
                        _settle_not_sent(reply, attempt, "provider_refused")
                        failure = DMSendGateError("provider_refused", reply.send_error)
                    else:
                        # No provider response, exception text or secret logs.
                        failure = DMSendUnknownError()
                else:
                    reply.status, reply.platform_reply_id, reply.send_error = InboxReply.Status.SENT, mid, ""
                    reply.sent_at = timezone.now()
                    if actor is not None and reply.author_id is None:
                        reply.author = actor
                    reply.save(
                        update_fields=["status", "platform_reply_id", "send_error", "sent_at", "author", "updated_at"]
                    )
                    attempt.outcome, attempt.reason_code = DMSendAttempt.Outcome.SENT, "provider_accepted"
                    attempt.completed_at = timezone.now()
                    attempt.save(update_fields=["outcome", "reason_code", "completed_at"])
                    from .conversations import record_reply

                    try:
                        with transaction.atomic():
                            record_reply(reply)
                    except Exception:
                        pass  # Optional projection cannot undo external acceptance.
    except Exception:
        # A failed transaction/commit cannot erase the earlier durable marker.
        # Do not claim that a failed DB write implies that HTTP did not happen.
        raise DMSendUnknownError() from None
    if failure is not None:
        raise failure
    # Local post-send bookkeeping cannot make a send retryable.
    with suppress(Exception):
        _apply_post_send_side_effects(message)
    return reply
