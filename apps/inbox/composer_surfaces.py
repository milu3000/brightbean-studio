"""One typed REST/MCP contract for canonical composer actions."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .composer_authorization import key_read_authorization
from .conversation_composer import (
    composer_context,
    retire_failed_conversation_reply,
    retire_unattempted_conversation_draft,
    save_conversation_draft,
    send_conversation_reply,
)
from .dm_send_gate import DMSendGateError, key_send_authorization
from .models import InboxConversation


def legacy_write_upgrade(key, message_id, request=None):
    """Machine-readable migration route; never creates an action or incoming."""
    from django.db.models import Q

    from .conversation_composer import enabled
    from .conversation_policy import read_allowed
    from .models import ConversationMessage, InboxMessage
    from .sync_identity import canonical_owns_account

    try:
        message_id = UUID(str(message_id))
    except (ValueError, TypeError, AttributeError):
        return None
    allowed = key.social_accounts.all().filter(workspace_id=key.workspace_id)
    legacy = (
        InboxMessage.objects.select_related("social_account")
        .filter(
            pk=message_id,
            workspace_id=key.workspace_id,
            social_account_id__in=allowed.values("pk"),
        )
        .first()
    )
    if legacy is not None and legacy.message_type != "dm":
        return None
    candidates = Q(pk=message_id) | Q(legacy_message_id=message_id)
    if legacy is not None:
        candidates |= Q(social_account_id=legacy.social_account_id, platform_message_id=legacy.platform_message_id)
    row = (
        ConversationMessage.objects.select_related("conversation", "social_account")
        .filter(
            candidates,
            workspace_id=key.workspace_id,
            social_account_id__in=allowed.values("pk"),
            direction="inbound",
        )
        .first()
    )
    account = row.social_account if row is not None else legacy.social_account if legacy is not None else None
    if account is None:
        return None
    authorization = key_read_authorization(key, request)
    authorization(account)
    conversation = row.conversation if row is not None else None
    if conversation is not None and (
        conversation.workspace_id != key.workspace_id
        or conversation.social_account_id != account.pk
        or conversation.platform != account.platform
    ):
        conversation = None
    if not (
        (enabled() and read_allowed(account))
        or canonical_owns_account(account)
        or (conversation is not None and conversation.composer_replies.exists())
    ):
        return None
    authorization(account)
    identifier = str(conversation.pk) if conversation else None
    return {
        "error": "canonical_composer_required",
        "contract_version": 2,
        "detail": "This account uses conversation actions. Read its composer and submit an explicit action nonce and revision.",
        "conversation_id": identifier,
        "canonical_read_tool": "get_conversation_messages" if identifier else "list_conversations",
        "composer_tool": "get_inbox_conversation_composer",
        "draft_tool": "save_inbox_conversation_draft",
        "send_tool": "send_inbox_conversation_reply",
        "retire_tool": "retire_inbox_conversation_reply",
        "composer_api": f"/api/v1/inbox/conversations/{identifier}/composer" if identifier else None,
        "same_nonce_is_retry": True,
        "new_nonce_is_new_message": True,
        "automatic_retry_allowed": False,
    }


class ConversationDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=10000, strict=True)
    action_nonce: UUID
    expected_revision: int = Field(ge=0, strict=True)
    scope_token: str = Field(min_length=1, max_length=4096, strict=True)
    adopt_reply_id: UUID | None = None
    observation_token: str | None = Field(
        default=None,
        max_length=4096,
        strict=True,
        description="Signed proof returned with the newest observed_conversation. Required for an owned send; save never refreshes it. Re-read actual newest history if stale.",
    )
    quote_target_id: UUID | None = Field(
        default=None, description="Canonical message UUID to quote; null or omission clears the quote."
    )


class ConversationRetireInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply_id: UUID
    expected_revision: int = Field(ge=0, strict=True)
    scope_token: str = Field(min_length=1, max_length=4096, strict=True)
    kind: Literal["draft", "failed"]


def conversation_for_key(key, conversation_id, request=None):
    row = (
        InboxConversation.objects.select_related("social_account")
        .filter(
            pk=conversation_id,
            workspace_id=key.workspace_id,
            social_account__workspace_id=key.workspace_id,
            social_account_id__in=key.social_accounts.all().values("pk"),
        )
        .first()
    )
    if row is None:
        raise DMSendGateError("not_found_or_denied", "This conversation is unavailable in the current account scope.")
    key_read_authorization(key, request)(row.social_account)
    return row


def serialize_composer(state):
    from apps.api.schemas import InboxReplyResponse

    return {
        "enabled": state["enabled"],
        "conversation_id": str(state["conversation"].pk) if state["conversation"] else None,
        "composer_revision": state["composer_revision"],
        "action_nonce": state["action_nonce"],
        "scope_token": state["scope_token"],
        "send_availability": state["send_availability"],
        "can_save_draft": state["can_save_draft"],
        "can_retire_failed": state["can_retire_failed"],
        "can_retire_draft": state["can_retire_draft"],
        "active_reply": InboxReplyResponse.from_reply(state["active_reply"]).model_dump(mode="json")
        if state["active_reply"]
        else None,
        "adopt_reply": InboxReplyResponse.from_reply(state["adopt_reply"]).model_dump(mode="json")
        if state["adopt_reply"]
        else None,
        "adopt_reply_id": state["adopt_reply_id"],
        "legacy_draft_ids": [str(reply.pk) for reply in state["legacy_drafts"]],
        "quote_supported": state["quote_supported"],
        "quote_editable": state["quote_editable"],
        "quote_target_id": state["quote_target_id"],
        "quote_preview": state["quote_preview"],
        "ownership": state["ownership"],
        "observation_token": state["observation_token"],
        "has_legacy_drafts": state["has_legacy_drafts"],
        "requires_legacy_adoption": state["requires_legacy_adoption"],
    }


def read_composer(*, key, conversation_id, request=None, adopt_reply_id=None, observation_token=None):
    from .canonical_reads import enabled as reads_enabled
    from .canonical_reads import read_conversation

    conversation = conversation_for_key(key, conversation_id, request)
    authorization = key_read_authorization(key, request)
    observed = None
    if observation_token is None and reads_enabled():
        # This GET returns the complete bounded newest page with its proof.
        # Mutation/error responses must never call this to renew an unseen proof.
        observed = read_conversation(authorization.canonical_scope(conversation.social_account), conversation.pk)
        observation_token = observed["composer_observation_token"]
    result = serialize_composer(
        composer_context(
            conversation,
            authorization=authorization,
            send_authorization=key_send_authorization(key, request),
            adopt_reply_id=adopt_reply_id,
            observation_token=observation_token,
            automated=True,
        )
    )
    if observed is not None:
        result["observed_conversation"] = observed
    authorization(conversation.social_account)
    return result


def mutate_composer(*, key, conversation_id, payload, action, request=None):
    from apps.api.schemas import InboxReplyResponse

    conversation = conversation_for_key(key, conversation_id, request)
    read_authorization = key_read_authorization(key, request)
    if action == "retire":
        value = ConversationRetireInput.model_validate(payload)
        retire = retire_unattempted_conversation_draft if value.kind == "draft" else retire_failed_conversation_reply
        reply = retire(message=conversation, authorization=read_authorization, **value.model_dump(exclude={"kind"}))
    else:
        value = ConversationDraftInput.model_validate(payload)
        arguments = {"message": conversation, "author": key.issued_by, **value.model_dump()}
        if action == "save":
            reply = save_conversation_draft(**arguments, authorization=read_authorization)
        elif action == "send":
            reply = send_conversation_reply(
                **arguments,
                draft_authorization=read_authorization,
                authorization=key_send_authorization(key, request),
                automated=True,
            )
        else:
            raise ValueError("Unsupported composer action.")
    read_authorization(conversation.social_account)
    return InboxReplyResponse.from_reply(reply).model_dump(mode="json")
