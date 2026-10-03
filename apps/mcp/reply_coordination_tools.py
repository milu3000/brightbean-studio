"""Read-only visibility into local-only V2 reply coordination.

No draft bodies, actor identities, idempotency keys or claim secrets are exposed.
This tool cannot prepare, pause, reserve or dispatch a reply.
"""

from typing import Any

from django.utils import timezone

from apps.inbox.conversation_policy import read_allowed, read_available
from apps.inbox.models import ConversationMessage, ConversationWorkState, InboxConversation, SendOperation
from apps.inbox.reply_coordination import enabled
from apps.mcp.conversation_tools import _iso, _scope, _sync
from apps.mcp.handlers import _parse_uuid, _wrap_text
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tools import Tool, register_tool


def _target(message_id, conversation, scoped):
    if message_id is None:
        return None
    return ConversationMessage.objects.filter(
        **scoped,
        pk=message_id,
        social_account_id=conversation.social_account_id,
        conversation_id=conversation.pk,
        direction="inbound",
    ).first()


def _get_reply_coordination(args: dict, context: dict[str, Any]) -> dict:
    _key, _accounts, scoped = _scope(context)
    if not enabled():
        raise JsonRpcError(INVALID_PARAMS, "Reply coordination is not enabled")
    conversation_id = _parse_uuid(args.get("conversation_id"), "conversation_id")
    try:
        conversation = (
            InboxConversation.objects.filter(**scoped).select_related("social_account").get(pk=conversation_id)
        )
    except InboxConversation.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Conversation not found") from exc
    if not read_allowed(conversation.social_account):
        raise JsonRpcError(INVALID_PARAMS, "Conversation not found")
    result: dict[str, Any] = {
        "conversation_id": str(conversation.pk),
        "social_account_id": str(conversation.social_account_id),
        "platform": conversation.platform,
        "conversation_revision": conversation.revision,
        "observed_at": _iso(timezone.now()),
        "coordination_status": "not_started",
        "work": None,
        "active_operation": None,
        "local_preflight_evaluated": False,
        "send_allowed": False,
        "provider_dispatch_enabled": False,
        "send_preconditions_enforced": False,
        "freshness_complete": False,
        "sync": _sync(conversation.social_account, scoped),
        "limitations": [
            "Read-only local coordination state; no provider dispatch is implemented.",
            "Existing send tools and per-message inbound events are unchanged and are not guarded by this coordinator.",
            "The deadline is not send authorization. Read and claim must recheck revisions and current permissions.",
            "Native outgoing can arrive late and does not establish human authorship or resolved work.",
            "Fields are observed local state, not an atomic snapshot or proof of complete platform history.",
        ],
    }
    authorized_conversations = InboxConversation.objects.filter(**scoped, pk=conversation.pk).values("pk")
    state = ConversationWorkState.objects.filter(conversation_id__in=authorized_conversations).first()
    active_ids = list(
        SendOperation.objects.filter(
            **scoped,
            social_account_id=conversation.social_account_id,
            conversation_id__in=authorized_conversations,
            status__in=["prepared", "claimed", "outcome_unknown"],
        ).values_list("pk", flat=True)[:2]
    )
    if state is None:
        if active_ids:
            result["coordination_status"] = "unavailable"
        return _wrap_text(result)

    # Defense in depth for corrupt/stale cross-account FKs, even when the actor
    # can independently access both accounts. Never project foreign references.
    latest = _target(state.latest_incoming_id, conversation, scoped)
    operation = None
    operation_target = None
    if state.active_operation_id:
        operation = SendOperation.objects.filter(
            **scoped,
            pk=state.active_operation_id,
            conversation_id__in=authorized_conversations,
            social_account_id=conversation.social_account_id,
        ).first()
        if operation:
            operation_target = _target(operation.target_id, conversation, scoped)
    invalid = (
        active_ids != ([state.active_operation_id] if state.active_operation_id else [])
        or (state.latest_incoming_id is not None and latest is None)
        or (state.active_operation_id is not None and (operation is None or operation_target is None))
    )
    if invalid:
        result["coordination_status"] = "unavailable"
        result["limitations"].append(
            "Coordination relationships could not be verified; no foreign references returned."
        )
        return _wrap_text(result)

    result["coordination_status"] = "observed"
    result["work"] = {
        "generation": state.generation,
        "conversation_revision": state.conversation_revision,
        "revision_matches": state.conversation_revision == conversation.revision,
        "owner_paused": state.owner_paused,
        "pause_reason": state.pause_reason
        if state.pause_reason in {"", "owner_requested", "outgoing_observed", "identity_uncertain", "snapshot_stale"}
        else "unknown",
        "identity_quarantined": state.identity_quarantined,
        "ordering_uncertain": state.ordering_uncertain,
        "history_gap": state.history_gap,
        "latest_incoming_id": str(latest.pk) if latest else None,
        "latest_incoming_deleted": latest.is_deleted if latest else None,
        "burst_started_at": _iso(state.burst_started_at),
        "latest_incoming_at": _iso(state.latest_incoming_at),
        "due_at": _iso(state.due_at),
        "timing_basis": "server_observation_time",
        "target_ordering": "provider_time_with_uncertainty_hold",
    }
    if operation and operation_target:
        result["active_operation"] = {
            "id": str(operation.pk),
            "status": operation.status,
            "target_message_id": str(operation_target.pk),
            "expected_revision": operation.expected_revision,
            "expected_generation": operation.expected_generation,
        }
    return _wrap_text(result)


class _CoordinationTool(Tool):
    def is_enabled(self) -> bool:
        return enabled() and read_available()


register_tool(
    _CoordinationTool(
        name="get_reply_coordination",
        description=(
            "Read local-only reply coordination for an authorized DM conversation: pause, burst deadline, "
            "generation and safe operation status. No provider dispatch is implemented. Does not prepare, "
            "claim, resume or send; existing send tools remain unguarded by this coordinator."
        ),
        input_schema={
            "type": "object",
            "properties": {"conversation_id": {"type": "string", "format": "uuid"}},
            "required": ["conversation_id"],
            "additionalProperties": False,
        },
        handler=_get_reply_coordination,
    )
)
