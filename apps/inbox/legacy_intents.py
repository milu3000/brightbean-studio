"""Legacy/public intent identity on existing reply rows and existing permissions.

No canonical conversation is invented for public threads. Context and nonce
metadata follow the existing reply's lifetime; no message content is copied.
"""

from contextlib import suppress
from uuid import UUID, uuid4

from django.db import transaction
from django.db.models import Case, F, Q, Value, When

from .canonical_access import digest
from .dm_send_gate import DMSendGateError, _authorize, _outermost_required
from .locking import lock_dm_account
from .models import InboxReply
from .presentation import stored_thread_messages


def identity(message):
    account = message.social_account
    return [
        str(message.workspace_id),
        str(account.pk),
        account.platform,
        account.account_platform_id,
        account.webhook_target_id,
        message.platform_message_id,
        message.message_type,
    ]


def context_digest(message):
    extra = message.extra if isinstance(message.extra, dict) else {}
    thread = stored_thread_messages(message)
    latest = thread.order_by("-received_at", "-pk").values_list("pk", "body", "extra").first()
    own = InboxReply.objects.filter(inbox_message__in=thread, status="sent")
    latest_own = (
        own.order_by(F("sent_at").desc(nulls_last=True), "-created_at", "-pk")
        .values_list("pk", "platform_reply_id", "body")
        .first()
    )
    latest_input = (
        [str(latest[0]), latest[1], latest[2].get("is_deleted") if isinstance(latest[2], dict) else None]
        if latest
        else None
    )
    own_proof = [own.count(), [str(latest_own[0]), latest_own[1], latest_own[2]] if latest_own else None]
    return digest(
        [
            identity(message),
            latest_input,
            own_proof,
            message.body,
            {
                key: extra.get(key)
                for key in (
                    "conversation_id",
                    "sender_id",
                    "sender",
                    "recipient_id",
                    "recipient",
                    "parent_id",
                    "post_id",
                    "is_deleted",
                )
            },
        ]
    )


def public_send_hold(message, *, exclude_reply_id=None):
    held = InboxReply.objects.filter(inbox_message__in=stored_thread_messages(message)).filter(
        Q(status="unknown") | Q(status="failed", not_sent_verified=False)
    )
    if exclude_reply_id is not None:
        held = held.exclude(pk=exclude_reply_id)
    return held.exists()


def read_action(message):
    from apps.api.schemas import InboxReplyResponse

    pending = (
        InboxReply.objects.filter(inbox_message__in=stored_thread_messages(message))
        .filter(Q(status__in=["draft", "unknown"]) | Q(status="failed", not_sent_verified=False))
        .select_related("inbox_message__social_account")
        .order_by(
            Case(
                When(Q(status="unknown") | Q(status="failed", not_sent_verified=False), then=Value(0)), default=Value(1)
            ),
            "-created_at",
            "-pk",
        )
        .first()
    )
    target = pending.inbox_message if pending else message
    payload = {
        "action_nonce": str(pending.legacy_action_nonce or uuid4()) if pending else str(uuid4()),
        "reply_id": str(pending.pk) if pending else None,
        "reply_revision": pending.updated_at.isoformat() if pending else None,
        "identity": identity(target),
        "context_digest": context_digest(target),
        "target_id": str(target.pk),
    }
    return payload, InboxReplyResponse.from_reply(pending).model_dump(mode="json") if pending else None


def validate_context(reply):
    if reply.legacy_action_nonce and reply.legacy_context_digest != context_digest(reply.inbox_message):
        raise DMSendGateError("stale_context", "Read the current conversation before sending this action.")


@transaction.atomic
def save_action(scope, message, payload, *, body, author):
    from .saved_reads import resolve_target
    from .services import create_reply_draft, update_reply_draft

    body = body.strip()
    if not body:
        raise DMSendGateError("empty_body", "Enter a reply body.")
    account = lock_dm_account(message.social_account_id, message.workspace_id)
    kind, current = resolve_target(scope, message.pk)
    if account is None or kind != "legacy" or identity(current) != payload["identity"]:
        raise DMSendGateError("scope_changed", "This saved reply target changed. Read it again.")
    nonce = UUID(payload["action_nonce"])
    existing = InboxReply.objects.select_for_update().filter(legacy_action_nonce=nonce).first()
    if existing is None and payload.get("reply_id"):
        existing = InboxReply.objects.select_for_update().filter(pk=payload["reply_id"], inbox_message=current).first()
        if existing is None:
            raise DMSendGateError("draft_changed", "This saved draft is no longer available.")
    if existing is not None:
        if (
            existing.inbox_message_id != current.pk
            or existing.conversation_id
            or existing.legacy_action_nonce not in {None, nonce}
        ):
            raise DMSendGateError("scope_changed", "This action no longer belongs to this target.")
        if current.message_type != "dm" and existing.status == "failed" and not existing.not_sent_verified:
            raise DMSendGateError(
                "legacy_outcome_unverified",
                "The earlier platform outcome is unverified. Review it before changing or sending this action.",
            )
        if existing.send_generation or existing.status in {"sent", "unknown"}:
            if existing.body != body.strip():
                raise DMSendGateError("attempted_action", "An attempted action cannot change its body.")
            return existing
        if payload.get("reply_revision") is None or (
            existing.updated_at.isoformat() != payload["reply_revision"] and existing.body != body.strip()
        ):
            raise DMSendGateError("stale_revision", "The saved draft changed. Read it again.")
    if context_digest(current) != payload["context_digest"]:
        raise DMSendGateError("stale_context", "New activity changed the conversation. Read it again.")
    if existing is None:
        existing = create_reply_draft(message=current, body=body, author=author)
    else:
        existing = update_reply_draft(existing, body=body)
    existing.legacy_action_nonce = nonce
    existing.legacy_context_digest = payload["context_digest"]
    existing.save(update_fields=["legacy_action_nonce", "legacy_context_digest", "updated_at"])
    return existing


def reference_after_save(message, reply, original):
    return {**original, "reply_id": str(reply.pk), "reply_revision": reply.updated_at.isoformat()}


def send_public_action(reply, *, actor, authorization, automated, dispatch_binding=None):
    """Commit uncertainty before transport; all old entry points use this too."""
    from django.utils import timezone

    from .dm_send_gate import DMSendUnknownError, _known_refusal
    from .models import SendOperation
    from .services import (
        _apply_post_send_side_effects,
        _dispatch_to_platform,
        _lock_reply_with_account,
        reply_failure_reason,
    )

    def check_public_route():
        if reply.dm_send_attempts.filter(outcome="unknown").exists():
            raise DMSendUnknownError()
        if (
            reply.inbox_message.message_type == "dm"
            or reply.dm_send_attempts.exists()
            or reply.conversation_id
            or dispatch_binding is not None
            or SendOperation.objects.filter(reply=reply).exists()
        ):
            raise DMSendGateError("route_changed", "This reply requires its existing DM send controls.")

    _outermost_required()
    with transaction.atomic(durable=True):
        _lock_reply_with_account(reply)
        check_public_route()
        if authorization is not None or reply.legacy_action_nonce:
            _authorize(authorization, reply.inbox_message.social_account)
        if automated and reply.inbox_message.social_account.connection_status != "connected":
            raise DMSendGateError("disconnected", "This account is disconnected.")
        if reply.status == "sent":
            return reply
        verified_retry = not reply.legacy_action_nonce and reply.status == "failed" and reply.not_sent_verified
        if not verified_retry and (reply.status != "draft" or reply.send_generation):
            raise DMSendGateError("attempted_action", "Review the original outcome; this action cannot be sent again.")
        validate_context(reply)
        from .reply_safety import has_follow_up_intent, validate_follow_up_parent

        if has_follow_up_intent(reply):
            if reply.follow_up_of_id is None:
                raise DMSendGateError("missing_parent", "The original reply is no longer available.")
            validate_follow_up_parent(reply.inbox_message, reply.follow_up_of, reply=reply)
        if public_send_hold(reply.inbox_message, exclude_reply_id=reply.pk):
            raise DMSendGateError(
                "outcome_unknown", "Another reply to this target has an unknown outcome. Do not resend."
            )
        if actor is not None and reply.author_id is None:
            reply.author = actor
        reply.status, reply.not_sent_verified, reply.send_error = "unknown", False, ""
        reply.send_generation += 1
        reply.save(
            update_fields=["status", "not_sent_verified", "send_generation", "send_error", "author", "updated_at"]
        )
        generation = reply.send_generation
    failure = None
    try:
        with transaction.atomic(durable=True):
            _lock_reply_with_account(reply)
            account = reply.inbox_message.social_account
            entered = False

            def before_provider():
                nonlocal entered
                check_public_route()
                if authorization is not None or reply.legacy_action_nonce:
                    _authorize(authorization, account)
                if automated and account.connection_status != "connected":
                    raise DMSendGateError("disconnected", "This account is disconnected.")
                validate_context(reply)
                if reply.status != "unknown" or reply.send_generation != generation:
                    raise DMSendGateError("receipt_changed", "This action's receipt changed.")
                entered = True

            try:
                before_provider()
                # entered marks only the actual adapter boundary, not preflight.
                entered = False
                mid = _dispatch_to_platform(
                    reply.inbox_message, reply.body, automated=automated, before_provider=before_provider
                )
                if not isinstance(mid, str) or not mid.strip() or len(mid) > 255:
                    entered = True
                    raise DMSendGateError("outcome_unknown", "The platform returned no trustworthy receipt.")
            except Exception as exc:
                known = not entered or isinstance(exc, NotImplementedError) or _known_refusal(exc, account.platform)
                if known:
                    reply.status, reply.not_sent_verified = "failed", True
                    reply.send_error = reply_failure_reason(exc)
                    reply.save(update_fields=["status", "not_sent_verified", "send_error", "updated_at"])
                if known and not reply.legacy_action_nonce and not isinstance(exc, DMSendGateError):
                    failure = exc if isinstance(exc, NotImplementedError) else RuntimeError(reply_failure_reason(exc))
                else:
                    failure = DMSendGateError("not_sent" if known else "outcome_unknown", reply_failure_reason(exc))
            else:
                reply.status, reply.platform_reply_id, reply.sent_at = "sent", mid, timezone.now()
                reply.send_error = ""
                reply.save(update_fields=["status", "platform_reply_id", "sent_at", "send_error", "updated_at"])
    except Exception:
        raise DMSendGateError("outcome_unknown", "The original delivery outcome is unknown. Do not resend.") from None
    if failure:
        raise failure
    with suppress(Exception):
        _apply_post_send_side_effects(reply.inbox_message)
    return reply
