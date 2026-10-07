"""Service layer for the Unified Social Inbox (F-3.1).

Both the HTMX views and the programmatic surfaces (the ``/api/v1/inbox``
REST router and the MCP inbox tools) go through these functions so the
three can't drift — the same rule the composer follows with
``apps.composer.services``.

A reply now has a lifecycle: it is created as a ``draft``, then a separate
send step delivers it to the platform and moves it to ``sent`` (or
``failed`` if the platform refused it). The platform-dispatch logic used
to live in ``apps/inbox/views.py``; it moved here verbatim.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from providers import get_provider

from .locking import lock_dm_account
from .models import InboxMessage, InboxReply, InboxSLAConfig

logger = logging.getLogger(__name__)

# Message types answered on a comment edge rather than a messaging endpoint.
_COMMENT_LIKE_TYPES = {
    InboxMessage.MessageType.COMMENT,
    InboxMessage.MessageType.MENTION,
    InboxMessage.MessageType.REVIEW,
}

# Extended replies require separately verified Meta feature approval and permitted use.
HUMAN_AGENT_AFTER = timedelta(hours=24)

# States a reply can be sent (or re-sent) from.
_SENDABLE_STATUSES = {InboxReply.Status.DRAFT, InboxReply.Status.FAILED}


class ReplyStateError(ValueError):
    """Raised when an operation is not valid for a reply's current status."""


def validate_automated_reply_window(message: InboxMessage) -> None:
    """Meta's HUMAN_AGENT exception is never available to an automated caller."""
    if message.message_type != InboxMessage.MessageType.DM or message.social_account.platform not in {
        "facebook",
        "instagram",
        "instagram_login",
    }:
        return
    received_at = message.received_at
    if not isinstance(received_at, datetime) or timezone.is_naive(received_at):
        raise ReplyStateError("Automated Meta replies require a valid inbound message timestamp.")
    age = timezone.now() - received_at
    if age < timedelta(0) or age >= HUMAN_AGENT_AFTER:
        raise ReplyStateError("Automated Meta replies are only allowed within 24 hours of the inbound message.")


def validate_meta_reply_window(message: InboxMessage, *, automated: bool) -> None:
    """No Meta DM surface may infer app feature approval from manual mode.

    Current account metadata records missing OAuth scopes, not Meta's Human
    Agent feature approval or permitted purpose. No empty scope list, setting,
    session identity or provider mock supplies that missing evidence.
    """
    from .dm_send_gate import DMSendGateError

    if message.message_type != InboxMessage.MessageType.DM or message.social_account.platform not in {
        "facebook",
        "instagram",
        "instagram_login",
    }:
        return
    if automated:
        validate_automated_reply_window(message)
        return
    received_at = message.received_at
    if not isinstance(received_at, datetime) or timezone.is_naive(received_at):
        raise DMSendGateError("invalid_reply_window", "This reply requires a verified incoming message timestamp.")
    age = timezone.now() - received_at
    if age < timedelta(0) or age >= timedelta(days=7):
        raise DMSendGateError("reply_window_closed", "The incoming message is outside the supported reply window.")
    if age >= HUMAN_AGENT_AFTER:
        raise DMSendGateError(
            "human_agent_permission_unverified",
            "Replies after 24 hours require verified platform Human Agent approval and permitted use; that approval is not verified for this account.",
        )


# ---------------------------------------------------------------------------
# Platform dispatch (moved from views.py, behaviour unchanged)
# ---------------------------------------------------------------------------


def reply_failure_reason(exc: Exception) -> str:
    """A short, actionable reason for the user.

    The UI/API receives a fixed category-specific sentence. DM dispatch logs
    only allowlisted numeric diagnostics, never raw provider text or JSON.
    """
    from .dm_send_gate import DMSendGateError
    from .provider_failures import provider_failure_reason

    if isinstance(exc, DMSendGateError):
        return str(exc)
    if isinstance(exc, NotImplementedError):
        return "this platform does not support sending this reply."

    return provider_failure_reason(exc)


def _dispatch_to_platform(
    message: InboxMessage, body: str, *, automated: bool = False, before_provider=None, reply=None
) -> str:
    """Post ``body`` back to the platform and return the platform's reply ID.

    Raises if the platform refuses it, so the caller can avoid recording a
    reply as delivered when it never was.
    """
    from apps.publisher.engine import _resolve_publish_credentials

    account = message.social_account
    validate_meta_reply_window(message, automated=automated)
    provider = get_provider(account.platform, _resolve_publish_credentials(account))

    # The messaging endpoints address a person, not a message, so carry the
    # sender's platform-scoped ID alongside the original payload.
    extra = dict(message.extra or {})
    if message.sender_handle:
        extra.setdefault("recipient_id", message.sender_handle)

    if message.message_type in _COMMENT_LIKE_TYPES:
        if before_provider is not None:
            before_provider()
        result = provider.reply_to_comment(
            access_token=account.oauth_access_token,
            comment_id=message.platform_message_id,
            text=body,
            extra=extra,
        )
    else:
        quote_kwargs = {}
        if reply is not None and reply.quote_target_id:
            if before_provider is None:
                from .reply_quotes import validate_quote

                validate_quote(reply, reply.conversation, account)
            # The shared before_provider gate validates this immutable quote
            # and fingerprint immediately before HTTP. Do not validate earlier
            # outside its known-not-sent exception boundary.
            quote_kwargs["reply_to_message_id"] = reply.quote_platform_message_id
        # Recheck after credentials resolve and immediately before provider dispatch.
        validate_meta_reply_window(message, automated=automated)
        overdue = not automated and timezone.now() - message.received_at > HUMAN_AGENT_AFTER
        if before_provider is not None:
            before_provider()
        result = provider.reply_to_message(
            access_token=account.oauth_access_token,
            message_id=message.platform_message_id,
            text=body,
            extra=extra,
            human_agent=overdue,
            **quote_kwargs,
        )

    return result.platform_message_id


def _apply_post_send_side_effects(message: InboxMessage) -> None:
    """Resolve or open the message after a reply goes out, per SLA config."""
    from .canonical_send_target import is_transport_projection

    if is_transport_projection(message):
        return
    sla_config = InboxSLAConfig.objects.filter(workspace=message.workspace, is_active=True).first()
    if sla_config and sla_config.auto_resolve_on_reply:
        if message.status != InboxMessage.Status.RESOLVED:
            message.status = InboxMessage.Status.RESOLVED
            message.save(update_fields=["status"])
    elif message.status == InboxMessage.Status.UNREAD:
        message.status = InboxMessage.Status.OPEN
        message.save(update_fields=["status"])


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


@transaction.atomic
def create_reply_draft(
    *, message: InboxMessage, body: str, author=None, follow_up_of: InboxReply | None = None
) -> InboxReply:
    """Create a ``draft`` reply against ``message``. Not sent anywhere."""
    body = (body or "").strip()
    if not body:
        raise ValueError("Reply body cannot be empty.")
    identity = (
        InboxMessage.objects.filter(pk=message.pk).values("message_type", "social_account_id", "workspace_id").first()
    )
    if identity is None:
        raise ReplyStateError("This incoming message is no longer available.")
    if identity["message_type"] == InboxMessage.MessageType.DM or follow_up_of is not None:
        account = lock_dm_account(identity["social_account_id"], identity["workspace_id"])
        current = InboxMessage.objects.select_for_update().get(pk=message.pk)
        if account is None or (
            current.message_type != message.message_type
            or current.workspace_id != message.workspace_id
            or current.social_account_id != message.social_account_id
            or current.social_account_id != account.pk
            or current.workspace_id != account.workspace_id
        ):
            raise ReplyStateError("The selected DM target changed; reload before drafting.")
        current.social_account = account
        from .reply_safety import check_dm_receipts, validate_follow_up_parent

        if follow_up_of is not None:
            existing = (
                InboxReply.objects.select_for_update()
                .filter(inbox_message=current, follow_up_of_id=follow_up_of.pk)
                .first()
            )
            if existing is not None and existing.status not in _SENDABLE_STATUSES:
                raise ReplyStateError("This sent reply already has a follow-up. Open that reply instead.")
            candidate = existing or InboxReply(inbox_message=current, follow_up_of=follow_up_of, is_follow_up=True)
            parent = validate_follow_up_parent(current, follow_up_of, reply=candidate)
            if current.message_type == InboxMessage.MessageType.DM:
                from .reply_dispatch import check_conversation_send

                check_dm_receipts(current, candidate, include_drafts=True, follow_up_of=parent)
                check_conversation_send(account, current, candidate)
            elif current.replies.filter(status=InboxReply.Status.UNKNOWN).exists():
                raise ReplyStateError(
                    "Delivery outcome is unknown. Do not create another reply for this incoming message."
                )
            if existing is not None:
                if existing.body == body and existing.author_id == getattr(author, "pk", None):
                    return existing
                raise ReplyStateError("This sent reply already has a follow-up draft. Open and edit that draft.")
            return InboxReply.objects.create(
                inbox_message=current,
                author=author,
                body=body,
                status=InboxReply.Status.DRAFT,
                follow_up_of=parent,
                is_follow_up=True,
            )

        check_dm_receipts(current)
        existing = InboxReply.objects.filter(inbox_message=current).order_by("created_at").first()
        if existing is not None:
            if (
                existing.status in _SENDABLE_STATUSES
                and existing.body == body
                and existing.author_id == getattr(author, "pk", None)
            ):
                return existing
            raise ReplyStateError("This incoming message already has a reply draft. Open and edit that draft.")
        message = current
    elif message.replies.filter(status=InboxReply.Status.UNKNOWN).exists():
        raise ReplyStateError("Delivery outcome is unknown. Do not create another reply for this incoming message.")
    return InboxReply.objects.create(
        inbox_message=message,
        author=author,
        body=body,
        status=InboxReply.Status.DRAFT,
    )


def _lock_reply(reply: InboxReply) -> None:
    """Refresh the caller's instance while locking the row for this transaction."""
    try:
        reply.refresh_from_db(from_queryset=InboxReply.objects.select_for_update())
    except InboxReply.DoesNotExist as exc:
        raise ReplyStateError("This reply has been discarded.") from exc


def _lock_reply_with_account(reply: InboxReply) -> None:
    """Edits/deletes share account -> reply ordering with send and retention."""
    identity = (
        InboxReply.objects.filter(pk=reply.pk)
        .values("inbox_message__social_account_id", "inbox_message__workspace_id")
        .first()
    )
    if identity is None:
        raise ReplyStateError("This reply has been discarded.")
    account = lock_dm_account(identity["inbox_message__social_account_id"], identity["inbox_message__workspace_id"])
    _lock_reply(reply)
    message = reply.inbox_message
    if account is None or message.social_account_id != account.pk or message.workspace_id != account.workspace_id:
        raise ReplyStateError("The reply account changed; reload before editing.")


@transaction.atomic
def update_reply_draft(reply: InboxReply, *, body: str) -> InboxReply:
    """Edit a draft (or failed) reply's body."""
    _lock_reply_with_account(reply)
    if reply.conversation_id:
        raise ReplyStateError("Use the conversation composer revision to edit this draft.")
    from .reply_safety import LEGACY_UNVERIFIED_MESSAGE, UNKNOWN_MESSAGE, is_unresolved_reply

    if is_unresolved_reply(reply):
        raise ReplyStateError(
            UNKNOWN_MESSAGE if reply.status == InboxReply.Status.UNKNOWN else LEGACY_UNVERIFIED_MESSAGE
        )
    if reply.dm_send_attempts.exists() or hasattr(reply, "send_operation"):
        raise ReplyStateError("Replies with durable DM attempts or V2 operations cannot be edited.")
    if reply.status not in _SENDABLE_STATUSES:
        raise ReplyStateError(f"A {reply.get_status_display().lower()} reply cannot be edited.")
    body = (body or "").strip()
    if not body:
        raise ValueError("Reply body cannot be empty.")
    reply.body = body
    reply.save(update_fields=["body", "updated_at"])
    return reply


@transaction.atomic
def discard_reply_draft(reply: InboxReply) -> None:
    """Delete a draft (or failed) reply. Sent replies are permanent."""
    _lock_reply_with_account(reply)
    if reply.conversation_id:
        raise ReplyStateError("Conversation action receipts must be preserved; use an explicit composer action.")
    from .reply_safety import LEGACY_UNVERIFIED_MESSAGE, UNKNOWN_MESSAGE, is_unresolved_reply

    if is_unresolved_reply(reply):
        raise ReplyStateError(
            UNKNOWN_MESSAGE if reply.status == InboxReply.Status.UNKNOWN else LEGACY_UNVERIFIED_MESSAGE
        )
    if reply.dm_send_attempts.exists() or hasattr(reply, "send_operation"):
        raise ReplyStateError("Replies with durable DM attempts or V2 operations cannot be discarded.")
    if reply.status not in _SENDABLE_STATUSES:
        raise ReplyStateError(f"A {reply.get_status_display().lower()} reply cannot be discarded.")
    reply.delete()


def send_reply_now(
    reply: InboxReply, *, actor=None, automated: bool = False, authorization=None, dispatch_binding=None
) -> InboxReply:
    """Common UI/REST/MCP boundary; persisted DM enrollment cannot be toggled off."""
    from .dm_send_gate import EnrolledDMSendError, capture_send_snapshot, send_enrolled_dm

    snapshot = capture_send_snapshot(reply)
    try:
        if snapshot is not None:
            from .reply_safety import send_unenrolled_dm

            return send_unenrolled_dm(
                reply,
                actor=actor,
                automated=automated,
                authorization=authorization,
                snapshot=snapshot,
                dispatch_binding=dispatch_binding,
            )
        return _send_legacy_reply(
            reply, actor=actor, automated=automated, snapshot=snapshot, dispatch_binding=dispatch_binding
        )
    except EnrolledDMSendError:
        return send_enrolled_dm(
            reply,
            actor=actor,
            automated=automated,
            authorization=authorization,
            snapshot=snapshot,
            dispatch_binding=dispatch_binding,
        )


def reply_send_availability(message, *, reply=None, follow_up_of: InboxReply | None = None):
    """Read-only common send status; dispatch still verifies current permissions."""
    from .reply_safety import reply_send_availability as availability

    return availability(message, reply=reply, follow_up_of=follow_up_of)


def _send_legacy_reply(
    reply: InboxReply, *, actor=None, automated: bool = False, snapshot=None, dispatch_binding=None
) -> InboxReply:
    """Deliver an existing draft/failed reply to the platform.

    On a platform refusal the row is kept and moved to ``failed`` with a
    human-readable ``send_error`` so the team can retry; the underlying
    exception is re-raised for the caller to shape into its own error.
    Automated callers must pass ``automated=True`` on every attempt, including
    retries. They cannot use the human-only Meta reply extension, and an
    unsupported provider is a failure for both human and automated callers.
    """
    failure: Exception | None = None
    with transaction.atomic():
        _lock_reply(reply)
        message = InboxMessage.objects.select_for_update().get(pk=reply.inbox_message_id)
        if message.replies.filter(status=InboxReply.Status.UNKNOWN).exclude(pk=reply.pk).exists():
            raise ReplyStateError("Delivery outcome is unknown. Do not retry this incoming message.")
        if reply.dm_send_attempts.filter(outcome="unknown").exists():
            from .dm_send_gate import DMSendUnknownError

            raise DMSendUnknownError()
        if message.message_type == InboxMessage.MessageType.DM or snapshot is not None:
            # Even a direct call to this compatibility path cannot bypass the
            # committed DM receipt or a persisted account/ownership gate.
            raise ReplyStateError("DM replies require the shared committed send boundary.")
        if dispatch_binding is not None or reply.dm_send_attempts.exists():
            raise ReplyStateError("This reply requires its existing DM send controls.")
        from .models import SendOperation

        if SendOperation.objects.filter(reply=reply, ownership__isnull=False).exists():
            raise ReplyStateError("This reply requires its bound dispatch operation.")
        if reply.status not in _SENDABLE_STATUSES:
            raise ReplyStateError(f"A {reply.get_status_display().lower()} reply cannot be sent again.")
        from .reply_safety import has_follow_up_intent, validate_follow_up_parent

        if has_follow_up_intent(reply):
            if reply.follow_up_of_id is None:
                raise ReplyStateError("This follow-up's original sent reply is no longer available; sending is held.")
            validate_follow_up_parent(message, reply.follow_up_of, reply=reply)
        if automated and message.social_account.connection_status != "connected":
            raise ReplyStateError("The social account is not connected. Reconnect it before sending.")
        if actor is not None and reply.author_id is None:
            reply.author = actor

        try:
            platform_reply_id = (
                _dispatch_to_platform(message, reply.body, automated=True)
                if automated
                else _dispatch_to_platform(message, reply.body)
            )
        except NotImplementedError as exc:
            reply.status = InboxReply.Status.FAILED
            reply.not_sent_verified = True
            reply.send_error = reply_failure_reason(exc)
            reply.save(update_fields=["status", "send_error", "author", "not_sent_verified", "updated_at"])
            failure = exc
        except Exception as exc:
            logger.exception("Failed to send inbox reply %s (%s)", reply.id, message.social_account.platform)
            reply.status = InboxReply.Status.FAILED
            reply.not_sent_verified = False
            reply.send_error = reply_failure_reason(exc)
            reply.save(update_fields=["status", "send_error", "author", "not_sent_verified", "updated_at"])
            failure = exc

        if failure is None:
            reply.status = InboxReply.Status.SENT
            reply.not_sent_verified = False
            reply.platform_reply_id = platform_reply_id
            reply.send_error = ""
            reply.sent_at = timezone.now()
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

            # Keep provider acceptance even if the optional history projection
            # fails; an externally sent message must never become sendable again.
            from .conversations import record_reply

            try:
                with transaction.atomic():
                    record_reply(reply)
            except Exception:
                logger.exception("Conversation history write failed for inbox reply %s", reply.id)

    # Raising inside atomic would roll back the failed status and its reason.
    if failure is not None:
        raise failure
    # SLA bookkeeping must not roll back a reply already delivered externally.
    try:
        _apply_post_send_side_effects(message)
    except Exception:
        logger.exception("Inbox reply %s was sent, but updating the message status failed", reply.id)
    return reply


def send_reply(
    *, message: InboxMessage, body: str, author=None, authorization=None, follow_up_of: InboxReply | None = None
) -> InboxReply:
    """Create a reply and send it in one step (the classic composer flow).

    DM receipts retain their actual outcome, including failed/unknown. An
    unsupported provider also keeps its failed row and never claims delivery.
    """
    with transaction.atomic():
        reply = create_reply_draft(message=message, body=body, author=author, follow_up_of=follow_up_of)
    try:
        return send_reply_now(reply, actor=author, authorization=authorization)
    except Exception as exc:
        if message.message_type != InboxMessage.MessageType.DM and not isinstance(exc, NotImplementedError):
            InboxReply.objects.filter(
                pk=reply.pk, status=InboxReply.Status.FAILED, dm_send_attempts__isnull=True
            ).delete()
        raise
