"""Canonical actions bundle the existing context proofs without renewing them.

The caller retains an opaque action reference. The same nonce is replayed;
claiming, ownership checks and dispatch still belong to the existing composer.
"""

from typing import Literal
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
    target_id: str = Field(
        min_length=1,
        max_length=1024,
        description="One saved conversation/message UUID or exact native ID. No account lookup required.",
    )
    content: Literal["body"] | None = Field(
        default=None,
        description="Request bounded full text for a message ID. Returns next_cursor for more text; never reads retained content.",
    )
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


def read_inbox(*, key, target_id, request=None, cursor=None, limit=30, content=None):
    scope = reader.key_read_scope(key, request)
    accounts, grants = scope.refresh()
    before = (sorted(str(pk) for pk in accounts.values_list("pk", flat=True)), grants)
    if content == "body":
        result = _read_body(scope, target_id, cursor=cursor)
    elif content is None:
        result = _read_inbox(scope, key=key, target_id=target_id, request=request, cursor=cursor, limit=limit)
    else:
        raise reader.CanonicalReadError("invalid_content", "Only bounded message body content can be requested.")
    accounts, grants = scope.refresh()
    if before != (sorted(str(pk) for pk in accounts.values_list("pk", flat=True)), grants):
        raise reader.CanonicalReadError("stale_scope", "This saved context changed while reading. Read it again.")
    result["conversation"].pop("composer_observation_token", None)
    _next_reads(result)
    return result


def _read_inbox(scope, *, key, target_id, request=None, cursor=None, limit=30):
    requested_reference = str(target_id)
    target_id = saved_reads.resolve_read_reference(scope, target_id)
    result = saved_reads.read_detail(scope, target_id, cursor=cursor, limit=limit)
    summary = _read_summary(scope, result, requested_reference)
    result["data"].pop("read_ack_token", None)
    if cursor:
        return {**summary, "conversation": result["data"], "reference": None, "draft": None}
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
        return {**summary, "conversation": result["data"], "reference": reference, "draft": draft}
    # The caller sees actual newest history with the proof. Mutations never
    # invoke this read to mint a fresh observation on the caller's behalf.
    observation = result["data"]["composer_observation_token"]
    state = read_composer(key=key, conversation_id=result["target_id"], request=request, observation_token=observation)
    summary["reply_status"] = {**state["send_availability"], "advisory_only": True, "send_authorized": False}
    return {
        **summary,
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


def _read_summary(scope, result, requested_reference):
    """Common read-only overview; reply eligibility is never send authorization."""
    from django.db.models import Q

    from .models import ConversationMessage, InboxConversation, InboxReply
    from .reply_safety import reply_send_availability

    data = result["data"]
    match = Q(platform_message_id=requested_reference)
    uuid: UUID | None
    try:
        uuid = UUID(requested_reference)
        match |= Q(pk=uuid)
    except ValueError:
        uuid = None
    anchor_message = None
    if result["kind"] == "canonical":
        conversation = InboxConversation.objects.select_related("social_account").get(pk=result["target_id"])
        account = conversation.social_account
        anchors = list(
            ConversationMessage.objects.filter(
                match | Q(legacy_message_id=uuid) | Q(legacy_reply_id=uuid) if uuid else match,
                conversation=conversation,
                workspace_id=scope.workspace_id,
                social_account=account,
            ).select_related("conversation")[:2]
        )
        if len(anchors) == 1:
            anchor_message = reader.project_message(anchors[0])
        thread = data["conversation"]
        replies = list(
            InboxReply.objects.filter(
                Q(conversation=conversation)
                | Q(conversation_message__conversation=conversation)
                | Q(
                    inbox_message_id__in=ConversationMessage.objects.filter(
                        conversation=conversation, social_account=account
                    ).values("legacy_message_id")
                ),
                inbox_message__workspace_id=scope.workspace_id,
                inbox_message__social_account_id=account.pk,
            )
            .order_by("-created_at", "-pk")
            .values("id", "status", "platform_reply_id", "sent_at")[:21]
        )
        for reply in replies:
            reply["id"] = str(reply["id"])
            reply["sent_at"] = reply["sent_at"].isoformat() if reply["sent_at"] else None
        eligibility = {
            "allowed": False,
            "code": "not_evaluated",
            "reason": "Eligibility is not evaluated on older pages; sending rechecks current permissions.",
        }
        coverage = data["coverage"]
    else:
        _kind, message = saved_reads.resolve_target(scope, result["target_id"])
        account = message.social_account
        from .presentation import stored_thread_messages
        from .thread_reads import _bounded_message

        anchors = list(stored_thread_messages(message).filter(match).select_related("social_account")[:2])
        if len(anchors) == 1:
            anchor_message = _bounded_message(anchors[0])
        thread = {"id": result["target_id"], "domain": message.message_type, "grouping": data["grouping"]}
        replies = list({r["id"]: r for item in data["messages"] for r in item["replies"]}.values())
        eligibility = reply_send_availability(message)
        coverage = {"coverage": "unknown", "outbound_coverage": data["outbound_coverage"]}
    anchor_receipt = None
    if uuid is not None:
        anchor_receipt = (
            InboxReply.objects.filter(
                pk=uuid, inbox_message__workspace_id=scope.workspace_id, inbox_message__social_account_id=account.pk
            )
            .values("id", "inbox_message_id", "status", "platform_reply_id", "sent_at")
            .first()
        )
        if anchor_receipt:
            anchor_receipt["id"] = str(anchor_receipt["id"])
            anchor_receipt["message_id"] = str(anchor_receipt.pop("inbox_message_id"))
            anchor_receipt["sent_at"] = anchor_receipt["sent_at"].isoformat() if anchor_receipt["sent_at"] else None
    return {
        "target_id": result["target_id"],
        "anchor_receipt": anchor_receipt,
        "anchor_message": anchor_message,
        "account": {
            "id": str(account.pk),
            "name": account.account_name,
            "handle": account.account_handle,
            "platform": account.platform,
        },
        "thread": thread,
        "existing_replies": replies[:20],
        "existing_replies_truncated": len(replies) > 20
        or (result["kind"] == "legacy" and bool(data["next_cursor"]))
        or any(item.get("replies_truncated") for item in data["messages"]),
        "reply_status": {**eligibility, "advisory_only": True, "send_authorized": False},
        "history": {
            "complete": False,
            "coverage": coverage,
            "next_cursor": data["next_cursor"],
            "undated_next_cursor": data.get("undated_next_cursor"),
            "limitations": [
                "Saved authorized history only; missing outgoing does not prove unanswered.",
                "More saved content requires the returned cursor; platform history may remain incomplete.",
                "Individual bodies and attachments may be previews; their truncation flags remain authoritative.",
            ],
        },
    }


def _read_body(scope, reference, *, cursor=None):
    """Continue default visible text through the same scoped read entry point."""
    target_id = saved_reads.resolve_read_reference(scope, reference)
    detail = saved_reads.read_detail(scope, target_id, limit=1)
    overview = _read_summary(scope, detail, str(reference))
    message = overview["anchor_message"]
    if detail["kind"] != "canonical":
        raise reader.CanonicalReadError(
            "content_unavailable",
            "This legacy/public item retains its existing get_inbox_message full-detail contract.",
        )
    if message is None:
        raise reader.CanonicalReadError(
            "message_reference_required",
            "Use the canonical message ID returned by read_inbox to read its full text.",
        )
    body = reader.read_message_body(scope, message["id"], cursor=cursor, limit=4000)
    if body["conversation_id"] != detail["target_id"]:
        raise reader.CanonicalReadError("stale_scope", "The message changed while reading; read it again.")
    return {
        "target_id": message["id"],
        "account": overview["account"],
        "thread": {"id": body["conversation_id"], "domain": "dm"},
        "conversation": {},
        "content": {"kind": "body", **body},
        "next_cursor": body["next_cursor"],
        "history": {"complete": False, "coverage": overview["history"]["coverage"]},
        "reference": None,
        "draft": None,
        "reply_status": {"allowed": False, "code": "not_evaluated", "advisory_only": True, "send_authorized": False},
    }


def _next_reads(result):
    """Ready-to-use read requests only; never execute them or infer coverage."""

    def call(tool, **arguments):
        return {"tool": tool, "arguments": arguments}

    pending = {}
    data = result.get("conversation", {})
    history = result.get("history", {})
    for purpose, field in (("history", "next_cursor"), ("undated_history", "undated_next_cursor")):
        if history.get(field):
            pending[purpose] = call(
                "read_inbox", target_id=result["target_id"], cursor=history[field], limit=data["limit"]
            )
    if result.get("content", {}).get("kind") == "body" and result.get("next_cursor"):
        pending["body"] = call(
            "read_inbox", target_id=result["target_id"], content="body", cursor=result["next_cursor"]
        )
    result["next_read"] = pending
    items = data.get("messages", []) + data.get("undated_messages", [])
    if result.get("anchor_message"):
        items = [result["anchor_message"], *items]
    for item in items:
        requests = {}
        canonical = item.get("id_namespace") == "canonical_message"
        if item.get("body_truncated"):
            requests["body"] = (
                call("read_inbox", target_id=item["id"], content="body")
                if canonical
                else call("get_inbox_message", message_id=item["id"])
            )
        if item.get("attachments_truncated"):
            requests["attachments"] = (
                call("get_conversation_attachments", message_id=item["id"], limit=3)
                if canonical
                else call("get_inbox_message", message_id=item["id"])
            )
        if item.get("replies_truncated") and not canonical:
            requests["replies"] = call("get_inbox_message", message_id=item["id"])
        item["next_read"] = requests
    receipt = result.get("anchor_receipt")
    if receipt:
        if data.get("source") == "canonical":
            receipt["next_read"] = (result.get("anchor_message") or {}).get("next_read", {})
        else:
            receipt["next_read"] = {"detail": call("get_inbox_message", message_id=receipt["message_id"])}
