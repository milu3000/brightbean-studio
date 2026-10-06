"""Shared DM receipt rules, using only already captured inbox data.

These checks do not enroll accounts or enable conversation capture. Keep this
module and the common send boundary when rolling back inbox presentation.
"""

from contextlib import suppress

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .locking import lock_dm_account
from .models import DMSendControl, InboxMessage, InboxReply, SendOperation

META_PLATFORMS = {"facebook", "instagram", "instagram_login"}
SEND_PLATFORMS = {"facebook", "instagram_login"}
UNKNOWN_MESSAGE = (
    "Delivery outcome is unknown. DM sends for this account are held; check the platform and do not retry."
)
_CONFLICT_REASONS = {
    "identity_conflict",
    "participant_endpoints_conflict",
    "participants_invalid",
    "participants_incomplete",
}
LEGACY_UNVERIFIED_MESSAGE = "An earlier failed DM has no verified delivery outcome. Sending is held; review the platform before taking further action."


def _error(code, reason):
    from .dm_send_gate import DMSendGateError

    return DMSendGateError(code, reason)


def has_follow_up_intent(reply):
    return reply.is_follow_up or bool(reply.follow_up_of_id)


def _follow_up_parent(reply):
    try:
        return reply.follow_up_of if reply is not None and reply.follow_up_of_id else None
    except InboxReply.DoesNotExist:
        return None


def validate_follow_up_parent(message, parent, *, reply=None):
    """Recheck an explicit sent receipt; never infer intent from retry text."""
    current = InboxReply.objects.select_related("inbox_message").filter(pk=parent.pk).first()
    if (
        current is None
        or current.pk == getattr(reply, "pk", None)
        or current.inbox_message_id != message.pk
        or current.inbox_message.workspace_id != message.workspace_id
        or current.inbox_message.social_account_id != message.social_account_id
        or current.status != InboxReply.Status.SENT
        or not current.platform_reply_id.strip()
        or current.sent_at is None
        or timezone.is_naive(current.sent_at)
        or current.sent_at > timezone.now()
        or (current.status, current.platform_reply_id, current.sent_at, current.inbox_message_id)
        != (parent.status, parent.platform_reply_id, parent.sent_at, parent.inbox_message_id)
    ):
        raise _error(
            "follow_up_parent_invalid", "The selected sent reply is no longer available for this incoming message."
        )
    child = InboxReply.objects.filter(follow_up_of=current).first()
    if child is not None and child.pk != getattr(reply, "pk", None):
        raise _error("follow_up_exists", "This sent reply already has a follow-up. Open that reply instead.")
    return current


def _recipient(message):
    from providers.meta_messaging import resolve_recipient_id

    extra = dict(message.extra) if isinstance(message.extra, dict) else {}
    if message.sender_handle:
        extra.setdefault("recipient_id", message.sender_handle)
    return resolve_recipient_id(extra)


def _native_conversation(message):
    extra = message.extra if isinstance(message.extra, dict) else {}
    value = extra.get("conversation_id")
    return value if isinstance(value, str) else ""


def _conflicting_classification(message):
    # Current ordinary polling can retain evidence after optional canonical
    # capture stops. An old direct projection must not hide newer group proof.
    extra = message.extra if isinstance(message.extra, dict) else {}
    kind, reason = message._conversation_classification()
    summary_reason = extra.get("classification_reason")
    return (
        extra.get("conversation_type") == "group"
        or (isinstance(summary_reason, str) and summary_reason in _CONFLICT_REASONS)
        or kind == "group"
        or reason in _CONFLICT_REASONS
    )


def is_unresolved_reply(reply):
    """Read-only uncertainty predicate for sending, editing and retention.

    Historic FAILED meant any exception, including possible provider acceptance.
    Only a current-service definitive failure marker or durable gate evidence
    proves otherwise. No historic row is retagged or cleared by this check.
    """
    if reply.status == InboxReply.Status.UNKNOWN:
        return True
    if reply.status != InboxReply.Status.FAILED or reply.inbox_message.message_type != InboxMessage.MessageType.DM:
        return False
    if reply.platform_reply_id or reply.sent_at:
        return True
    attempts = list(reply.dm_send_attempts.all())
    if attempts:
        return any(attempt.outcome != "not_sent" for attempt in attempts)
    return not reply.not_sent_verified


def validate_dm_target(message):
    """Require supported, verified direct evidence and an unambiguous peer."""
    account = message.social_account
    if getattr(settings, "INBOX_DM_SENDS_ENABLED", True) is not True:
        raise _error(
            "sending_paused", "Direct message sending is temporarily paused. Your drafts and receipts are preserved."
        )
    if message.message_type != InboxMessage.MessageType.DM:
        raise _error("target_changed", "The selected DM target changed; reload before sending.")
    from providers.meta_inbox_content import is_deleted_content

    extra = message.extra if isinstance(message.extra, dict) else {}
    nested = extra.get("message") if isinstance(extra.get("message"), dict) else {}
    if (
        is_deleted_content(extra)
        or extra.get("is_echo")
        or extra.get("is_self")
        or extra.get("direction") == "outbound"
        or nested.get("is_echo")
    ):
        raise _error("not_incoming", "This message is deleted or is not an incoming message; sending is held.")
    if account.platform in META_PLATFORMS:
        if message.conversation_type != "direct" or _conflicting_classification(message):
            raise _error("not_verified_direct", "Sending requires a verified one-to-one conversation.")
        peer = _recipient(message)
        if (
            not peer
            or peer in {account.account_platform_id, account.webhook_target_id}
            or (message.sender_handle and message.sender_handle != peer)
        ):
            raise _error("recipient_changed", "The DM recipient is missing or conflicts with the sender.")
        from providers.meta_inbox_content import classify_conversation_identity

        if any(key in extra for key in ("participants", "participant_ids")):
            kind, _reason, verified_peer = classify_conversation_identity(
                extra,
                own_ids=[account.account_platform_id, account.webhook_target_id],
                sender_id=message.sender_handle,
            )
            if kind != "direct" or peer != verified_peer:
                raise _error("recipient_changed", "The DM recipient does not match the verified conversation.")
        row = getattr(message, "conversation_message", None)
        if row is not None and (
            row.direction != "inbound"
            or row.is_deleted
            or row.social_account_id != account.pk
            or row.workspace_id != message.workspace_id
            or row.platform != account.platform
            or row.sender_id != peer
            or row.platform_message_id != message.platform_message_id
            or (
                row.conversation_id
                and (
                    row.conversation.peer_id != peer
                    or row.conversation.peer_ambiguous
                    or row.conversation.workspace_id != message.workspace_id
                    or row.conversation.social_account_id != account.pk
                    or row.conversation.platform != account.platform
                )
            )
        ):
            raise _error("target_changed", "The current conversation no longer verifies this incoming DM.")
        native_id = _native_conversation(message)
        if native_id:
            siblings = (
                InboxMessage.objects.filter(
                    social_account_id=account.pk,
                    workspace_id=message.workspace_id,
                    message_type=InboxMessage.MessageType.DM,
                    extra__conversation_id=native_id,
                )
                .exclude(pk=message.pk)
                .select_related("social_account", "conversation_message__conversation")
            )
            for sibling in siblings:
                if _conflicting_classification(sibling):
                    raise _error(
                        "thread_identity_conflict",
                        "This conversation has conflicting participant evidence; sending is held.",
                    )
    if account.platform not in SEND_PLATFORMS:
        raise _error("unsupported", "This account does not support sending direct messages.")
    if account.connection_status != "connected":
        raise _error("disconnected", "The social account is not connected. Reconnect it before sending.")


def check_dm_receipts(message, reply=None, *, include_drafts=False, follow_up_of=None):
    """Account lock serializes this predicate with draft creation and sends."""
    replies = InboxReply.objects.filter(inbox_message=message)
    if reply is not None and (
        (reply.follow_up_of_id and not reply.is_follow_up)
        or (follow_up_of is not None and reply.follow_up_of_id != follow_up_of.pk)
    ):
        raise _error("follow_up_parent_invalid", "The explicit follow-up intent does not match this draft.")
    parent = follow_up_of or _follow_up_parent(reply)
    if reply is not None and has_follow_up_intent(reply) and parent is None:
        raise _error(
            "follow_up_parent_missing", "This follow-up's original sent reply is no longer available; sending is held."
        )
    if parent is not None:
        validate_follow_up_parent(message, parent, reply=reply)
    if reply is not None and reply.status == InboxReply.Status.FAILED and is_unresolved_reply(reply):
        raise _error("legacy_outcome_unverified", LEGACY_UNVERIFIED_MESSAGE)
    if reply is not None:
        replies = replies.exclude(pk=reply.pk)
    if parent is None and replies.filter(status=InboxReply.Status.SENT).exists():
        raise _error("target_answered", "This incoming message already has a sent reply. Review the existing reply.")
    unknowns = InboxReply.objects.filter(
        inbox_message__social_account_id=message.social_account_id,
        status=InboxReply.Status.UNKNOWN,
    )
    if reply is not None:
        unknowns = unknowns.exclude(pk=reply.pk)
    # The existing reply has no immutable account/thread snapshot. Matching on
    # mutable incoming metadata could release uncertainty after a poll/edit;
    # hold the account just as the enrolled gate does, without enrolling it.
    if unknowns.exists():
        raise _error("outcome_unknown", UNKNOWN_MESSAGE)
    unverified = InboxReply.objects.select_related("inbox_message").filter(
        inbox_message__social_account_id=message.social_account_id,
        inbox_message__message_type=InboxMessage.MessageType.DM,
        status=InboxReply.Status.FAILED,
    )
    if reply is not None:
        unverified = unverified.exclude(pk=reply.pk)
    if any(is_unresolved_reply(candidate) for candidate in unverified):
        raise _error("legacy_outcome_unverified", LEGACY_UNVERIFIED_MESSAGE)
    if include_drafts and replies.filter(status__in=[InboxReply.Status.DRAFT, InboxReply.Status.FAILED]).exists():
        raise _error("existing_draft", "This incoming message already has a reply draft. Open and edit that draft.")
    operations = SendOperation.objects.filter(target__legacy_message=message).filter(
        Q(status__in=["prepared", "claimed", "outcome_unknown", "confirmed"]) | Q(external_attempted_at__isnull=False)
    )
    if reply is not None:
        operations = operations.exclude(reply=reply)
    if operations.exists():
        raise _error("existing_operation", "This incoming message already has a coordinated reply. Review it first.")


def reply_send_availability(message, *, reply=None, follow_up_of=None):
    """Advisory only: sending always locks and rechecks current authorization."""
    result = {"allowed": True, "code": "ready", "reason": "", "existing_reply_id": None}
    existing = message.replies.order_by("created_at").first()
    if follow_up_of is not None:
        existing = InboxReply.objects.filter(inbox_message=message, follow_up_of_id=follow_up_of.pk).first()
        if reply is None and existing is not None:
            reply = existing
    if existing:
        result["existing_reply_id"] = str(existing.pk)
    try:
        if reply is not None and reply.status not in {InboxReply.Status.DRAFT, InboxReply.Status.FAILED}:
            raise _error(
                "outcome_unknown"
                if reply.status == InboxReply.Status.UNKNOWN
                else "follow_up_exists"
                if follow_up_of is not None
                else "target_answered",
                UNKNOWN_MESSAGE if reply.status == InboxReply.Status.UNKNOWN else "This reply was already sent.",
            )
        if message.message_type != InboxMessage.MessageType.DM:
            parent = follow_up_of or _follow_up_parent(reply)
            if reply is not None and has_follow_up_intent(reply) and parent is None:
                raise _error(
                    "follow_up_parent_missing",
                    "This follow-up's original sent reply is no longer available; sending is held.",
                )
            if parent is not None:
                validate_follow_up_parent(message, parent, reply=reply)
            if message.replies.filter(status=InboxReply.Status.UNKNOWN).exists():
                raise _error("outcome_unknown", UNKNOWN_MESSAGE)
            return result
        validate_dm_target(message)
        check_dm_receipts(message, reply, include_drafts=True, follow_up_of=follow_up_of)
        from .dm_send_gate import _check_identity, _valid_time
        from .reply_dispatch import check_conversation_send

        control = DMSendControl.objects.filter(social_account_id=message.social_account_id).first()
        if control:
            _check_identity(control, message.social_account)
            if control.paused:
                raise _error("paused", "BrightBean DM sending is paused for this account.")
            if control.attempts.filter(outcome="unknown").exists():
                raise _error("outcome_unknown", "Delivery outcome is unknown. DM sends for this account are held.")
            cutoff = control.resume_cutoff or control.coverage_from
            times = [message.received_at, message.created_at]
            if reply is not None:
                times.append(reply.created_at)
            if not all(_valid_time(value, cutoff, timezone.now()) for value in times):
                raise _error(
                    "old_target", "This incoming message or draft predates the current send window; sending is held."
                )
        check_conversation_send(message.social_account, message, reply or InboxReply(inbox_message=message))
    except ValueError as exc:
        result.update(allowed=False, code=getattr(exc, "code", "held"), reason=str(exc))
    return result


def _lock_current(reply, snapshot):
    from .dm_send_gate import check_snapshot_identity
    from .services import _lock_reply

    account = lock_dm_account(snapshot.account_id, snapshot.workspace_id)
    if account is None:
        raise _error("target_changed", "The DM account changed; reload before sending.")
    check_snapshot_identity(snapshot, account)
    _lock_reply(reply)
    message = InboxMessage.objects.select_for_update().get(pk=reply.inbox_message_id)
    if message.social_account_id != account.pk or message.workspace_id != account.workspace_id:
        raise _error("target_changed", "The originally selected DM target changed; reload before sending.")
    message.social_account = account
    return account, message


def _check_current(reply, account, message, snapshot, authorization, automated):
    from .dm_send_gate import _authorize, _fingerprint
    from .reply_dispatch import check_conversation_send
    from .services import validate_automated_reply_window

    if DMSendControl.objects.filter(social_account=account).exists():
        raise _error("gate_changed", "The account send controls changed; review this reply before sending.")
    check_conversation_send(account, message, reply)
    if reply.dm_send_attempts.exists():
        raise _error("legacy_attempt", "This reply requires its existing durable DM send gate.")
    if automated:
        validate_automated_reply_window(message)
    validate_dm_target(message)
    check_dm_receipts(message, reply, include_drafts=True)
    if _fingerprint(reply, message) != snapshot.fingerprint or not reply.body.strip():
        raise _error("target_changed", "The selected DM target or draft changed; reload before sending.")
    _authorize(authorization, account)


def _prepare_receipt(reply, snapshot, authorization, automated, dispatch_binding):
    from .dm_send_gate import EnrolledDMSendError, _outermost_required

    _outermost_required()
    with transaction.atomic(durable=True):
        account, message = _lock_current(reply, snapshot)
        if DMSendControl.objects.filter(social_account=account).exists():
            raise EnrolledDMSendError()
        if dispatch_binding is not None:
            raise _error("ownership_required", "V2 dispatch requires its existing account send gate.")
        if reply.status not in {InboxReply.Status.DRAFT, InboxReply.Status.FAILED}:
            raise _error(
                "outcome_unknown" if reply.status == InboxReply.Status.UNKNOWN else "reply_state",
                UNKNOWN_MESSAGE if reply.status == InboxReply.Status.UNKNOWN else "This reply cannot be sent again.",
            )
        _check_current(reply, account, message, snapshot, authorization, automated)
        generation = reply.send_generation + 1
        reply.send_generation = generation
        reply.status, reply.send_error, reply.not_sent_verified = InboxReply.Status.UNKNOWN, UNKNOWN_MESSAGE, False
        reply.save(update_fields=["status", "send_error", "not_sent_verified", "send_generation", "updated_at"])
    # This committed marker survives worker death and final-receipt DB failure.
    return generation


def _check_send_generation(reply, generation):
    if not InboxReply.objects.filter(
        pk=reply.pk, status=InboxReply.Status.UNKNOWN, send_generation=generation
    ).exists():
        raise _error("receipt_changed", "This reply was reviewed or changed; reload its current result.")


def send_unenrolled_dm(reply, *, snapshot, actor, authorization, automated, dispatch_binding):
    from .dm_send_gate import _known_refusal
    from .services import ReplyStateError, _apply_post_send_side_effects, _dispatch_to_platform, reply_failure_reason

    generation = _prepare_receipt(reply, snapshot, authorization, automated, dispatch_binding)
    failure = None
    try:
        with transaction.atomic(durable=True):
            account, message = _lock_current(reply, snapshot)
            entered_provider = False
            # Status alone has an ABA hole: manual not-sent followed by a new
            # retry returns to UNKNOWN. Only this durable generation may send.
            _check_send_generation(reply, generation)
            try:
                _check_current(reply, account, message, snapshot, authorization, automated)

                def before_provider():
                    nonlocal entered_provider
                    _check_send_generation(reply, generation)
                    _check_current(reply, account, message, snapshot, authorization, automated)
                    entered_provider = True

                mid = _dispatch_to_platform(message, reply.body, automated=automated, before_provider=before_provider)
                if not isinstance(mid, str) or not mid.strip() or len(mid) > 255:
                    # A successful adapter return without its receipt is ambiguous.
                    entered_provider = True
                    raise _error("outcome_unknown", UNKNOWN_MESSAGE)
            except Exception as exc:
                if getattr(exc, "code", None) == "receipt_changed":
                    raise
                if not entered_provider or _known_refusal(exc, account.platform):
                    if not entered_provider and isinstance(exc, ReplyStateError):
                        failure = exc
                    elif not entered_provider and isinstance(exc, NotImplementedError):
                        failure = _error("unsupported", "This account does not support sending direct messages.")
                    else:
                        failure = _error("not_sent", f"DM send stopped: {reply_failure_reason(exc)}")
                    reply.status = InboxReply.Status.FAILED
                    reply.send_error, reply.not_sent_verified = str(failure), True
                    reply.save(update_fields=["status", "send_error", "not_sent_verified", "updated_at"])
                else:
                    failure = _error("outcome_unknown", UNKNOWN_MESSAGE)
            else:
                reply.status, reply.platform_reply_id, reply.send_error = InboxReply.Status.SENT, mid, ""
                reply.not_sent_verified = False
                reply.sent_at = timezone.now()
                if actor is not None and reply.author_id is None:
                    reply.author = actor
                reply.save(
                    update_fields=[
                        "status",
                        "platform_reply_id",
                        "send_error",
                        "sent_at",
                        "author",
                        "not_sent_verified",
                        "updated_at",
                    ]
                )
                from .conversations import record_reply

                with suppress(Exception), transaction.atomic():
                    record_reply(reply)
    except Exception as exc:
        if getattr(exc, "code", None) == "receipt_changed":
            raise
        raise _error("outcome_unknown", UNKNOWN_MESSAGE) from None
    if failure is not None:
        raise failure
    with suppress(Exception):
        _apply_post_send_side_effects(message)
    return reply
