"""One explicit outgoing action per nonce in the canonical conversation.

Reconstructed implementation. All provider dispatch remains in the existing
committed receipt boundary. No feature flag creates ownership or permission.
"""

from uuid import UUID, uuid4

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .canonical_send_target import latest_inbound, transport_target, validate_anchor, validate_anchor_identity
from .composer_authorization import key_read_authorization, session_read_authorization  # noqa: F401
from .conversation_policy import read_allowed
from .dm_send_gate import DMSendGateError, _authorize
from .locking import lock_dm_account
from .models import ConversationMessage, DMSendAttempt, InboxConversation, InboxMessage, InboxReply, SendOperation

_SALT = "inbox.conversation-composer.scope.v1"


def enabled():
    return getattr(settings, "INBOX_CONVERSATION_COMPOSER_ENABLED", False) is True


def _uuid(value, name):
    try:
        if isinstance(value, bool):
            raise ValueError
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise DMSendGateError("invalid_input", f"A valid {name} UUID is required.") from None


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DMSendGateError("invalid_revision", "A valid composer revision is required.")
    return value


def _generation(account):
    try:
        from .sync_identity import canonical_read_connection
    except ModuleNotFoundError as exc:
        if exc.name != "apps.inbox.sync_identity":
            raise
        return None
    connection = canonical_read_connection(account)
    return connection.generation if connection is not None else None


def _resolve(message, *, lock=False):
    if isinstance(message, InboxConversation):
        conversation_id = message.pk
    elif isinstance(message, InboxMessage) and message.message_type == "dm":
        row = ConversationMessage.objects.filter(
            legacy_message_id=message.pk,
            social_account_id=message.social_account_id,
            workspace_id=message.workspace_id,
            platform=message.social_account.platform,
        ).first()
        conversation_id = row.conversation_id if row else None
    else:
        conversation_id = None
    if not conversation_id:
        raise DMSendGateError("canonical_missing", "This message has no verified canonical conversation.")
    account = (
        lock_dm_account(message.social_account_id, message.workspace_id)
        if lock
        else message.social_account.__class__.objects.filter(
            pk=message.social_account_id, workspace_id=message.workspace_id
        ).first()
    )
    if account is None:
        raise DMSendGateError("identity_changed", "This account changed. Reload the conversation.")
    queryset = InboxConversation.objects.select_for_update() if lock else InboxConversation.objects
    conversation = queryset.filter(
        pk=conversation_id,
        workspace_id=account.workspace_id,
        social_account=account,
        platform=account.platform,
    ).first()
    if conversation is None:
        raise DMSendGateError("identity_changed", "The canonical conversation is outside the current account.")
    conversation.social_account = account
    return account, conversation


def _validate_conversation(account, conversation, *, require_enabled=True):
    if require_enabled and not enabled():
        raise DMSendGateError("composer_disabled", "Conversation drafting is not enabled.")
    if not read_allowed(account):
        raise DMSendGateError("canonical_unavailable", "This account's canonical conversation is not available.")
    if (
        account.platform not in {"facebook", "instagram_login"}
        or account.connection_status != "connected"
        or conversation.conversation_type != "direct"
        or conversation.peer_ambiguous
        or not conversation.peer_id
        or conversation.peer_id in {account.account_platform_id, account.webhook_target_id}
        or not conversation.platform_conversation_id
        or conversation.identity_kind != "platform"
        or not account.account_platform_id
    ):
        raise DMSendGateError(
            "not_verified_direct", "A connected, verified one-to-one native conversation is required."
        )


def _scope(account, conversation):
    from .composer_dispatch import scope_facts

    value = [
        str(conversation.pk),
        str(account.workspace_id),
        str(account.pk),
        account.platform,
        account.account_platform_id,
        account.webhook_target_id,
        conversation.platform_conversation_id,
        conversation.peer_id,
        str(_generation(account)),
    ]
    owner = scope_facts(conversation)
    return value + [owner] if owner is not None else value


def _check_scope(token, account, conversation, *, include_owner=True):
    try:
        actual = signing.loads(token, salt=_SALT, max_age=86400)
    except (signing.BadSignature, TypeError, ValueError):
        raise DMSendGateError("scope_changed", "Reload this conversation before editing or sending.") from None
    expected = _scope(account, conversation)
    if not isinstance(actual, list) or (actual != expected if include_owner else actual[:9] != expected[:9]):
        raise DMSendGateError("scope_changed", "This conversation's native identity changed. Reload it.")


def _validate_reply_intent(reply, conversation, account):
    if (
        reply.conversation_id != conversation.pk
        or reply.action_nonce is None
        or reply.inbox_message.workspace_id != account.workspace_id
        or reply.inbox_message.social_account_id != account.pk
        or reply.account_platform_id != account.account_platform_id
        or reply.recipient_id != conversation.peer_id
        or reply.platform_conversation_id != conversation.platform_conversation_id
        or reply.connection_generation != _generation(account)
    ):
        raise DMSendGateError("intent_changed", "The outgoing action no longer matches this native conversation.")


def _operation_not_sent(operation):
    from .reply_safety import is_unresolved_reply

    if operation.status != "failed" or not operation.reply_id:
        return False
    receipt = operation.reply
    attempt = DMSendAttempt.objects.filter(operation=operation, reply=receipt, outcome="not_sent").first()
    return bool(
        attempt
        and attempt.pk == operation.attempt_id
        and attempt.completed_at is not None
        and operation.external_attempted_at is not None
        and attempt.control.social_account_id == operation.social_account_id
        and attempt.control.workspace_id == operation.workspace_id
        and attempt.control.platform == operation.platform
        and receipt.inbox_message.workspace_id == operation.workspace_id
        and receipt.send_generation > 0
        and not receipt.dm_send_attempts.exclude(outcome="not_sent").exists()
        and (operation.conversation_action_nonce is None or operation.conversation_action_nonce == receipt.action_nonce)
        and receipt.status == "failed"
        and not is_unresolved_reply(receipt)
        and receipt.inbox_message.social_account_id == operation.social_account_id
    )


def check_conversation_receipts(conversation, reply=None):
    from .reply_safety import LEGACY_UNVERIFIED_MESSAGE, UNKNOWN_MESSAGE, is_unresolved_reply

    receipts = InboxReply.objects.select_related("inbox_message").filter(
        Q(inbox_message__social_account_id=conversation.social_account_id)
        | Q(conversation__social_account_id=conversation.social_account_id)
    )
    if reply is not None:
        receipts = receipts.exclude(pk=reply.pk)
        if reply.status == "failed" and is_unresolved_reply(reply):
            raise DMSendGateError("legacy_outcome_unverified", LEGACY_UNVERIFIED_MESSAGE)
    if receipts.filter(status="unknown").exists():
        raise DMSendGateError("outcome_unknown", UNKNOWN_MESSAGE)
    if any(is_unresolved_reply(candidate) for candidate in receipts.filter(status="failed")):
        raise DMSendGateError("legacy_outcome_unverified", LEGACY_UNVERIFIED_MESSAGE)
    attempts = DMSendAttempt.objects.filter(
        control__social_account_id=conversation.social_account_id, outcome="unknown"
    )
    if reply is not None:
        attempts = attempts.exclude(reply=reply)
    if attempts.exists():
        raise DMSendGateError("outcome_unknown", UNKNOWN_MESSAGE)
    operations = SendOperation.objects.filter(conversation=conversation).select_related("reply__inbox_message")
    if reply is not None:
        operations = operations.exclude(reply=reply)
    for operation in operations:
        if operation.status in {"prepared", "claimed", "outcome_unknown"} or (
            operation.external_attempted_at and operation.status != "confirmed" and not _operation_not_sent(operation)
        ):
            raise DMSendGateError("existing_operation", "A coordinated outgoing action still requires review.")


def validate_conversation_reply(message, reply, *, check_quote=True):
    account, conversation = _resolve(message)
    _validate_conversation(account, conversation)
    _validate_reply_intent(reply, conversation, account)
    if reply.retired_at or conversation.active_reply_id != reply.pk:
        raise DMSendGateError("inactive_action", "This outgoing action is no longer the active conversation draft.")
    row = ConversationMessage.objects.filter(legacy_message_id=reply.inbox_message_id).first()
    validate_anchor(row, conversation, account)
    check_conversation_receipts(conversation, reply)
    if check_quote:
        from .reply_quotes import validate_quote

        validate_quote(reply, conversation, account)
    return conversation


def _legacy_drafts(conversation):
    return InboxReply.objects.filter(
        inbox_message__conversation_message__conversation=conversation,
        inbox_message__workspace_id=conversation.workspace_id,
        inbox_message__social_account_id=conversation.social_account_id,
        inbox_message__social_account__workspace_id=conversation.workspace_id,
        inbox_message__social_account__platform=conversation.platform,
        conversation__isnull=True,
        status__in=["draft", "failed"],
    ).order_by("created_at", "pk")


def _editable(reply):
    from .receipt_compaction import reply_display_content

    return bool(
        reply.status == "draft"
        and reply.send_generation == 0
        and not reply.dm_send_attempts.exists()
        and not hasattr(reply, "send_operation")
        and not reply.retired_at
        and not reply.content_compacted_at
        and reply_display_content(reply)["available"]
    )


def composer_context(
    message,
    *,
    authorization=None,
    send_authorization=None,
    adopt_reply_id=None,
    observation_token=None,
    automated=False,
):
    state = {
        "enabled": enabled(),
        "conversation": None,
        "active_reply": None,
        "composer_revision": 0,
        "action_nonce": str(uuid4()),
        "scope_token": "",
        "legacy_drafts": [],
        "has_legacy_drafts": False,
        "requires_legacy_adoption": False,
        "quote_supported": False,
        "quote_editable": False,
        "quote_target_id": "",
        "quote_preview": None,
        "can_retire_failed": False,
        "can_retire_draft": False,
        "ownership": None,
        "observation_token": observation_token,
        "can_save_draft": False,
        "adopt_reply": None,
        "adopt_reply_id": "",
        "send_availability": {
            "allowed": False,
            "code": "composer_disabled",
            "reason": "Conversation drafting is not enabled.",
        },
    }
    if not enabled():
        return state
    try:
        account, conversation = _resolve(message)
        _authorize(authorization, account)
        _validate_conversation(account, conversation)
        active = conversation.active_reply
        state.update(
            conversation=conversation,
            composer_revision=conversation.composer_revision,
            scope_token=signing.dumps(_scope(account, conversation), salt=_SALT),
            legacy_drafts=list(_legacy_drafts(conversation)[:10]),
            has_legacy_drafts=_legacy_drafts(conversation).exists(),
        )
        state["requires_legacy_adoption"] = bool(
            active is None
            and _legacy_drafts(conversation).filter(status="draft").exists()
            and not conversation.composer_replies.exists()
            and not adopt_reply_id
        )
        from .reply_quotes import quote_preview, supported, validate_quote

        state["quote_supported"] = supported(account)
        state["quote_editable"] = supported(account) and (active is None or _editable(active))
        if active:
            _validate_reply_intent(active, conversation, account)
            state["active_reply"] = active
            state["action_nonce"] = str(active.action_nonce)
            state["quote_target_id"] = str(active.quote_target_id) if active.quote_target_id else ""
            state["quote_preview"] = quote_preview(active, conversation, account)
            from .composer_dispatch import can_retire_local_operation

            state["can_retire_draft"] = bool(
                active.status == "draft"
                and active.send_generation == 0
                and not active.dm_send_attempts.exists()
                and can_retire_local_operation(active, authorization)
            )
            check_conversation_receipts(conversation, active)
            from .reply_safety import is_unresolved_reply

            operation = SendOperation.objects.filter(reply=active).first()
            state["can_retire_failed"] = bool(
                active.status == "failed"
                and not is_unresolved_reply(active)
                and (operation is None or _operation_not_sent(operation))
            )
            state["can_save_draft"] = _editable(active)
            if not _editable(active):
                raise DMSendGateError(
                    "receipt_review", "Review the existing outgoing receipt before composing a new message."
                )
        if adopt_reply_id:
            selected = _legacy_drafts(conversation).filter(pk=_uuid(adopt_reply_id, "draft")).first()
            if active is not None or selected is None or not _editable(selected):
                raise DMSendGateError(
                    "adoption_unavailable", "The selected historical draft is not available for adoption."
                )
            from copy import copy

            from .receipt_compaction import reply_display_content

            content = reply_display_content(selected)
            if not content["available"]:
                raise DMSendGateError("adoption_unavailable", "The selected historical draft content is unavailable.")
            selected = copy(selected)
            selected.body, selected.send_error = content["body"], content["send_error"]
            state.update(adopt_reply=selected, adopt_reply_id=str(selected.pk))
        check_conversation_receipts(conversation, active)
        if active:
            validate_quote(active, conversation, account)
        row = latest_inbound(conversation, account)
        target = transport_target(row, conversation, account)
        from .reply_dispatch import check_conversation_send
        from .reply_safety import validate_dm_target

        state["can_save_draft"] = True
        validate_dm_target(target)
        _authorize(send_authorization or authorization, account)
        from .services import validate_meta_reply_window

        validate_meta_reply_window(target, automated=automated)
        from .composer_dispatch import availability

        ownership = availability(
            account,
            conversation,
            send_authorization or authorization,
            observation_token,
            draft_authorization=authorization,
        )
        state["ownership"] = ownership
        if ownership is None:
            check_conversation_send(account, target, active or InboxReply(inbox_message=target))
        elif not ownership["allowed"]:
            raise DMSendGateError(ownership["code"], ownership["reason"])
        if state["requires_legacy_adoption"]:
            raise DMSendGateError("adoption_required", "Select the historical draft to continue composing.")
        state["send_availability"] = {"allowed": True, "code": "ready", "reason": ""}
        state["can_save_draft"] = True
    except ValueError as exc:
        state["send_availability"] = {"allowed": False, "code": getattr(exc, "code", "held"), "reason": str(exc)}
    if state["conversation"] is not None:
        try:
            account, current = _resolve(message)
            _authorize(authorization, account)
            _check_scope(state["scope_token"], account, current)
            if (current.revision, current.composer_revision) != (
                state["conversation"].revision,
                state["composer_revision"],
            ):
                raise DMSendGateError("scope_changed", "This conversation changed while it was read. Reload it.")
        except ValueError as exc:
            state.update(
                active_reply=None,
                adopt_reply=None,
                legacy_drafts=[],
                quote_preview=None,
                quote_target_id="",
                scope_token="",
                can_save_draft=False,
                quote_editable=False,
                can_retire_draft=False,
                can_retire_failed=False,
                send_availability={"allowed": False, "code": getattr(exc, "code", "held"), "reason": str(exc)},
            )
    return state


@transaction.atomic
def save_conversation_draft(
    *,
    message,
    body,
    action_nonce,
    expected_revision,
    scope_token,
    author=None,
    authorization=None,
    adopt_reply_id=None,
    quote_target_id=None,
    observation_token=None,
):
    from .reply_quotes import parse_quote_id, pinned_quote, requested_quote, resolve_quote, set_quote

    quote_id = parse_quote_id(quote_target_id)
    nonce = _uuid(action_nonce, "action nonce")
    expected_revision = _revision(expected_revision)
    if not isinstance(body, str) or not body.strip():
        raise DMSendGateError("empty_body", "Reply body cannot be empty.")
    body = body.strip()
    account, conversation = _resolve(message, lock=True)
    _authorize(authorization, account)
    _validate_conversation(account, conversation)
    existing = InboxReply.objects.select_for_update().filter(conversation=conversation, action_nonce=nonce).first()
    _check_scope(
        scope_token, account, conversation, include_owner=not (existing is not None and existing.status == "sent")
    )
    if existing is not None:
        _validate_reply_intent(existing, conversation, account)
        if existing.retired_at:
            raise DMSendGateError("retired_action", "This action is retired. Use a new explicit action nonce.")
        if not _editable(existing):
            if existing.body != body or existing.quote_target_id != quote_id:
                raise DMSendGateError(
                    "action_nonce_reused",
                    "A frozen action cannot change. Use a new explicit action after its result is known.",
                )
            if existing.status == "sent":
                return existing
            operation = SendOperation.objects.filter(reply=existing).first()
            if (
                existing.status == "draft"
                and not existing.send_generation
                and not existing.dm_send_attempts.exists()
                and operation is not None
                and operation.conversation_action_nonce == existing.action_nonce
                and not operation.external_attempted_at
                and not operation.attempt_id
            ):
                return existing
            from .reply_safety import is_unresolved_reply

            if is_unresolved_reply(existing):
                raise DMSendGateError(
                    "outcome_unknown", "Delivery is unknown or unverified. Review it before any further action."
                )
            raise DMSendGateError(
                "receipt_review", "Explicitly retire the verified failed action before composing its successor."
            )
        if conversation.active_reply_id != existing.pk:
            raise DMSendGateError("inactive_action", "This draft is no longer the active conversation composer.")
        # Content-independent validation permits editing/clearing a draft after
        # withdrawal. The actual send boundary remains strict.
        anchor = ConversationMessage.objects.filter(legacy_message_id=existing.inbox_message_id).first()
        validate_anchor_identity(anchor, conversation, account)
        check_conversation_receipts(conversation, existing)
        quote = resolve_quote(conversation, account, quote_id)
        target = existing.inbox_message
        try:
            latest = latest_inbound(conversation, account)
        except DMSendGateError:
            # Keep local editing/cancellation possible when sending is held.
            latest = None
        if latest is not None and latest.pk != anchor.pk:
            target = transport_target(latest, conversation, account, materialize=True)
        if (
            quote_id is not None
            and existing.quote_target_id == quote_id
            and pinned_quote(existing) != requested_quote(quote)
        ):
            raise DMSendGateError("quote_changed", "The quote identity changed. Clear and select it again.")
        if (
            existing.body == body
            and pinned_quote(existing) == requested_quote(quote)
            and existing.inbox_message_id == target.pk
        ):
            return existing
        if conversation.composer_revision != expected_revision:
            raise DMSendGateError("stale_revision", "The draft changed in another tab. Reload before saving.")
        existing.body = body
        existing.inbox_message = target
        set_quote(existing, quote)
        existing.save(
            update_fields=[
                "body",
                "inbox_message",
                "quote_target",
                "quote_platform_message_id",
                "quote_platform_conversation_id",
                "quote_connection_generation",
                "updated_at",
            ]
        )
        conversation.composer_revision += 1
        conversation.save(update_fields=["composer_revision", "updated_at"])
        return existing
    if conversation.composer_revision != expected_revision:
        raise DMSendGateError("stale_revision", "The conversation composer changed. Reload before creating an action.")
    if conversation.active_reply_id:
        raise DMSendGateError("active_draft", "Open the active conversation draft before composing another message.")
    check_conversation_receipts(conversation)
    legacy = _legacy_drafts(conversation)
    adopted = None
    if (legacy.filter(status="draft").exists() or adopt_reply_id) and (
        adopt_reply_id or not conversation.composer_replies.exists()
    ):
        if not adopt_reply_id:
            raise DMSendGateError(
                "adoption_required", "Select a historical draft to adopt into this conversation composer."
            )
        adopted = legacy.filter(pk=_uuid(adopt_reply_id, "draft")).first()
        if adopted is None or not _editable(adopted):
            raise DMSendGateError("adoption_unavailable", "Only an unattempted historical draft can be adopted.")
    elif adopt_reply_id:
        raise DMSendGateError("adoption_unavailable", "The selected historical draft is not in this conversation.")
    row = latest_inbound(conversation, account)
    target = transport_target(row, conversation, account, materialize=True)
    from .composer_dispatch import owner_for
    from .reply_dispatch import check_conversation_send

    if owner_for(conversation) is None:
        check_conversation_send(account, target, adopted or InboxReply(inbox_message=target))
    reply = adopted or InboxReply(inbox_message=target, author=author)
    if adopted:
        reply.inbox_message = target
    reply.conversation = conversation
    reply.action_nonce = nonce
    reply.account_platform_id = account.account_platform_id
    reply.recipient_id = conversation.peer_id
    reply.platform_conversation_id = conversation.platform_conversation_id
    reply.connection_generation = _generation(account)
    reply.conversation_incoming_generation = conversation.incoming_generation
    reply.body = body
    set_quote(reply, resolve_quote(conversation, account, quote_id))
    reply.save()
    conversation.active_reply = reply
    conversation.composer_revision += 1
    conversation.save(update_fields=["active_reply", "composer_revision", "updated_at"])
    return reply


def send_conversation_reply(
    *, actor=None, automated=False, draft_authorization=None, authorization=None, observation_token=None, **kwargs
):
    from .conversation_workflow import record_composer_outcome
    from .services import send_reply_now, validate_meta_reply_window

    account, conversation = _resolve(kwargs["message"])
    _authorize(authorization, account)
    reply = save_conversation_draft(**kwargs, authorization=draft_authorization)
    if reply.status == "sent":
        return reply
    validate_meta_reply_window(reply.inbox_message, automated=automated)
    try:
        from .composer_dispatch import owner_for, send_owned

        if owner_for(reply.conversation) is not None:
            return send_owned(
                reply,
                authorization=authorization,
                draft_authorization=draft_authorization,
                observation_token=observation_token,
                scope_token=kwargs["scope_token"],
                actor=actor or kwargs.get("author"),
                automated=automated,
            )
        if observation_token:
            from .composer_dispatch import bind_observed_generation

            bind_observed_generation(reply, draft_authorization, observation_token)
        return send_reply_now(
            reply, actor=actor or kwargs.get("author"), automated=automated, authorization=authorization
        )
    finally:
        reply.refresh_from_db()
        if reply.status == "sent":
            # Receipt settlement clears its composer slot even when workflow
            # presentation is disabled; it grants no new dispatch permission.
            with transaction.atomic():
                _account, conversation = _resolve(kwargs["message"], lock=True)
                if conversation.active_reply_id == reply.pk:
                    conversation.active_reply = None
                    conversation.composer_revision += 1
                    conversation.save(update_fields=["active_reply", "composer_revision", "updated_at"])
        record_composer_outcome(reply)


@transaction.atomic
def retire_failed_conversation_reply(*, message, reply_id, expected_revision, scope_token, authorization):
    account, conversation = _resolve(message, lock=True)
    _authorize(authorization, account)
    _validate_conversation(account, conversation)
    _check_scope(scope_token, account, conversation)
    if conversation.composer_revision != _revision(expected_revision):
        raise DMSendGateError(
            "stale_revision", "The conversation composer changed. Review it before retiring this receipt."
        )
    reply = (
        InboxReply.objects.select_for_update().filter(pk=_uuid(reply_id, "reply"), conversation=conversation).first()
    )
    if reply is None or conversation.active_reply_id != reply.pk:
        raise DMSendGateError("inactive_action", "This failed receipt is not the active conversation action.")
    _validate_reply_intent(reply, conversation, account)
    from .reply_safety import is_unresolved_reply

    if reply.status != "failed" or is_unresolved_reply(reply):
        raise DMSendGateError("unverified_outcome", "Only a verified not-sent receipt can be retired.")
    operation = SendOperation.objects.filter(reply=reply).first()
    if operation and not _operation_not_sent(operation):
        raise DMSendGateError("existing_operation", "The coordinated action is not verified not-sent.")
    check_conversation_receipts(conversation, reply)
    reply.retired_at = timezone.now()
    reply.save(update_fields=["retired_at", "updated_at"])
    conversation.active_reply = None
    conversation.composer_revision += 1
    conversation.save(update_fields=["active_reply", "composer_revision", "updated_at"])
    return reply


@transaction.atomic
def retire_unattempted_conversation_draft(*, message, reply_id, expected_revision, scope_token, authorization):
    """Release a local draft slot without deleting text, identity, or nonce."""
    account, conversation = _resolve(message, lock=True)
    _authorize(authorization, account)
    _validate_conversation(account, conversation)
    _check_scope(scope_token, account, conversation)
    if conversation.composer_revision != _revision(expected_revision):
        raise DMSendGateError("stale_revision", "The conversation composer changed. Reload before closing this draft.")
    reply = (
        InboxReply.objects.select_for_update().filter(pk=_uuid(reply_id, "reply"), conversation=conversation).first()
    )
    if reply is None or conversation.active_reply_id != reply.pk:
        raise DMSendGateError("inactive_action", "This is not the active conversation draft.")
    _validate_reply_intent(reply, conversation, account)
    if reply.status != "draft" or reply.send_generation or reply.dm_send_attempts.exists():
        raise DMSendGateError(
            "attempted_action", "A draft with a delivery attempt cannot be closed as an unattempted draft."
        )
    from .composer_dispatch import retire_local_operation

    retire_local_operation(reply, authorization)
    reply.retired_at = timezone.now()
    reply.save(update_fields=["retired_at", "updated_at"])
    conversation.active_reply = None
    conversation.composer_revision += 1
    conversation.save(update_fields=["active_reply", "composer_revision", "updated_at"])
    return reply
