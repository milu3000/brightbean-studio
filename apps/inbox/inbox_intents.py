"""Canonical actions bundle the existing context proofs without renewing them.

The caller retains an opaque action reference. The same nonce is replayed;
claiming, ownership checks and dispatch still belong to the existing composer.
"""

from uuid import UUID

from django.core import signing
from pydantic import BaseModel, ConfigDict, Field

from . import canonical_reads as reader
from . import saved_reads
from .composer_surfaces import ConversationDraftInput, conversation_for_key, mutate_composer, read_composer
from .dm_send_gate import DMSendGateError

SALT = "inbox.intent-action.v1"


class InboxReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: UUID
    cursor: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=30, ge=1, le=50)


class InboxActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=32768)
    body: str = Field(min_length=1, max_length=10000, strict=True)
    quote_target_id: UUID | None = None


class InboxStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=32768)


def _reference(scope, state, observation):
    return signing.dumps(
        {
            "principal": scope.principal,
            "conversation_id": state["conversation_id"],
            "payload": {
                "action_nonce": state["action_nonce"],
                "expected_revision": state["composer_revision"],
                "scope_token": state["scope_token"],
                "observation_token": observation,
            },
        },
        salt=SALT,
        compress=True,
    )


def _decode(scope, reference, *, max_age=3600):
    try:
        value = signing.loads(reference, salt=SALT, max_age=max_age)
        if value["principal"] != scope.principal:
            raise ValueError
        # Existing signed scope and observation proofs remain authoritative.
        payload = value["payload"]
        if value.get("kind") == "legacy":
            UUID(payload["action_nonce"])
            if not isinstance(payload["identity"], list) or not isinstance(payload["context_digest"], str):
                raise ValueError
            return UUID(value["target_id"]), {**payload, "kind": "legacy"}
        ConversationDraftInput.model_validate({**payload, "body": "validation only"})
        return UUID(value["conversation_id"]), payload
    except (signing.BadSignature, ValueError, TypeError, KeyError) as exc:
        raise DMSendGateError(
            "stale_context", "Read this conversation again before changing its draft or sending."
        ) from exc


def read_inbox(*, key, target_id, request=None, cursor=None, limit=30):
    scope = reader.key_read_scope(key, request)
    result = saved_reads.read_detail(scope, target_id, cursor=cursor, limit=limit)
    result["data"].pop("read_ack_token", None)
    if cursor:
        return {"conversation": result["data"], "reference": None, "draft": None}
    if result["kind"] == "legacy":
        from .legacy_intents import context_digest, identity, read_action

        _kind, target = saved_reads.resolve_target(scope, target_id)
        payload, draft = read_action(target)
        final_kind, final_target = saved_reads.resolve_target(scope, payload["target_id"])
        if (
            final_kind != "legacy"
            or identity(final_target) != payload["identity"]
            or context_digest(final_target) != payload["context_digest"]
        ):
            raise DMSendGateError("scope_changed", "This saved context changed while reading. Read it again.")
        reference = signing.dumps(
            {"principal": scope.principal, "kind": "legacy", "target_id": payload["target_id"], "payload": payload},
            salt=SALT,
            compress=True,
        )
        return {"conversation": result["data"], "reference": reference, "draft": draft}
    # The caller sees actual newest history with the proof. Mutations never
    # invoke this read to mint a fresh observation on the caller's behalf.
    observation = result["data"]["composer_observation_token"]
    state = read_composer(key=key, conversation_id=result["target_id"], request=request, observation_token=observation)
    return {
        "conversation": result["data"],
        "draft": state["active_reply"],
        "reference": _reference(scope, state, observation) if state["enabled"] and state["scope_token"] else None,
        "send_availability": state["send_availability"],
    }


def mutate_inbox(*, key, reference, body, action, request=None, quote_target_id=None):
    scope = reader.key_read_scope(key, request)
    conversation_id, payload = _decode(scope, reference)
    if payload.get("kind") == "legacy":
        from apps.api.schemas import InboxReplyResponse

        from .composer_authorization import key_read_authorization
        from .dm_send_gate import _authorize, key_send_authorization
        from .legacy_intents import reference_after_save, save_action
        from .services import send_reply_now

        if quote_target_id is not None:
            raise DMSendGateError("unsupported_quote", "This saved thread does not support native quotes.")
        kind, target = saved_reads.resolve_target(scope, conversation_id)
        if kind != "legacy":
            raise DMSendGateError("scope_changed", "Read this conversation again with its current controls.")
        key_read_authorization(key, request)(target.social_account)
        reply = save_action(scope, target, payload, body=body, author=key.issued_by)
        if action == "send":
            authorization = key_send_authorization(key, request)
            _authorize(authorization, target.social_account)
            if reply.status != "sent":
                reply = send_reply_now(reply, actor=key.issued_by, authorization=authorization, automated=True)
        else:
            updated = reference_after_save(target, reply, payload)
            updated.pop("kind", None)
            reference = signing.dumps(
                {"principal": scope.principal, "kind": "legacy", "target_id": str(target.pk), "payload": updated},
                salt=SALT,
                compress=True,
            )
        return {"receipt": InboxReplyResponse.from_reply(reply).model_dump(mode="json"), "reference": reference}
    result = mutate_composer(
        key=key,
        conversation_id=conversation_id,
        request=request,
        action=action,
        payload={**payload, "body": body, "quote_target_id": quote_target_id},
    )
    if action == "save":
        # Saving advances the draft revision only. Retain the originally seen
        # history proof; a new incoming/outgoing still requires an explicit read.
        state = read_composer(
            key=key, conversation_id=conversation_id, request=request, observation_token=payload["observation_token"]
        )
        reference = _reference(scope, state, payload["observation_token"])
    return {"receipt": result, "reference": reference}


def inbox_send_status(*, key, reference, request=None):
    from apps.api.schemas import InboxReplyResponse

    scope = reader.key_read_scope(key, request)
    # Receipt lookup remains a current-authorized read after the write context
    # expires. This reference cannot authorize a new send or draft mutation.
    conversation_id, payload = _decode(scope, reference, max_age=None)
    if payload.get("kind") == "legacy":
        from .legacy_intents import identity
        from .models import InboxReply

        kind, target = saved_reads.resolve_target(scope, conversation_id)
        if kind != "legacy" or identity(target) != payload["identity"]:
            raise DMSendGateError("scope_changed", "This original reply target changed.")
        reply = InboxReply.objects.filter(inbox_message=target, legacy_action_nonce=payload["action_nonce"]).first()
        if reply is None and payload.get("reply_id"):
            reply = InboxReply.objects.filter(pk=payload["reply_id"], inbox_message=target).first()
        final_kind, final_target = saved_reads.resolve_target(scope, conversation_id)
        if final_kind != "legacy" or identity(final_target) != payload["identity"]:
            raise DMSendGateError("scope_changed", "This original reply target changed while reading.")
        if reply is None:
            return {"receipt": None, "transport_state": "not_attempted"}
        return {
            "receipt": InboxReplyResponse.from_reply(reply).model_dump(mode="json"),
            "transport_state": "provider_accepted"
            if reply.status == "sent" and reply.platform_reply_id
            else reply.status,
        }
    conversation = conversation_for_key(key, conversation_id, request)
    reply = conversation.composer_replies.filter(action_nonce=payload["action_nonce"]).first()
    if reply is None:
        return {"receipt": None, "transport_state": "not_attempted"}
    result = InboxReplyResponse.from_reply(reply).model_dump(mode="json")
    # The status read carries no send, acknowledgement or workflow transition.
    conversation_for_key(key, conversation_id, request)
    return {
        "receipt": result,
        "transport_state": "provider_accepted" if reply.status == "sent" and reply.platform_reply_id else reply.status,
    }
