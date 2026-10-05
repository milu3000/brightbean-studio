"""Default-off, current-principal conversation dispatch adapters.

No tool enrolls an account, grants access, transfers an owner, or starts an
unattended sender. Old tools are unchanged for conversations without ownership.
"""

from django.core.exceptions import PermissionDenied

from apps.inbox import reply_coordination, reply_dispatch
from apps.inbox.dispatch_access import (
    dispatch_actor,
    operation_result,
    require_request_permissions,
    visible_operation,
)
from apps.inbox.services import ReplyStateError
from apps.mcp.handlers import _parse_uuid, _wrap_text
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tools import Tool, register_tool


def _access(context):
    if not reply_dispatch.dispatch_enabled():
        raise JsonRpcError(INVALID_PARAMS, "Conversation dispatch is not enabled")
    try:
        require_request_permissions(context.get("membership"))
    except PermissionDenied as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return dispatch_actor(context["api_key"], context.get("request"))


def _held(exc):
    return JsonRpcError(INVALID_PARAMS, f"Conversation reply held ({getattr(exc, 'code', 'dispatch_held')})")


def _prepare(args, context):
    scope, authorization = _access(context)
    values = dict(args)
    for field in ("conversation_id", "social_account_id", "target_message_id"):
        values[field] = _parse_uuid(values.get(field), field)
    try:
        operation = reply_dispatch.prepare_owned_reply(scope, authorization=authorization, **values)
        return _wrap_text(operation_result(operation))
    except (reply_coordination.ReplyCoordinationError, ReplyStateError) as exc:
        raise _held(exc) from exc


def _claim(args, context):
    scope, authorization = _access(context)
    operation_id = _parse_uuid(args.get("operation_id"), "operation_id")
    try:
        visible_operation(scope, operation_id, authorization)
        operation = reply_dispatch.claim_owned_reply(scope, operation_id=operation_id, authorization=authorization)
        return _wrap_text(operation_result(operation, include_claim=True))
    except (reply_coordination.ReplyCoordinationError, ReplyStateError) as exc:
        raise _held(exc) from exc


def _dispatch(args, context):
    scope, authorization = _access(context)
    values = dict(args)
    for field in ("operation_id", "claim_token"):
        values[field] = _parse_uuid(values.get(field), field)
    try:
        visible_operation(scope, values["operation_id"], authorization)
        operation = reply_dispatch.dispatch_reply(
            scope, authorization=authorization, actor=context["api_key"].issued_by, **values
        )
        return _wrap_text(operation_result(operation))
    except (reply_coordination.ReplyCoordinationError, ReplyStateError) as exc:
        raise _held(exc) from exc


def _get_operation(args, context):
    scope, authorization = _access(context)
    operation_id = _parse_uuid(args.get("operation_id"), "operation_id")
    try:
        return _wrap_text(operation_result(visible_operation(scope, operation_id, authorization)))
    except (reply_coordination.ReplyCoordinationError, ReplyStateError) as exc:
        raise _held(exc) from exc


_UUID = {"type": "string", "format": "uuid"}
_REVISION = {"type": "integer", "minimum": 0}
_EPOCH = {"type": "integer", "minimum": 1}


def _register(name, description, handler, properties):
    register_tool(
        Tool(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
            handler=handler,
            enabled_predicate=reply_dispatch.dispatch_enabled,
        )
    )


_register(
    "prepare_conversation_reply",
    "Prepare a V2 reply only for a conversation explicitly owned by this current principal. Requires inbox read/send "
    "permission, exact owner epoch, observed conversation revision/generation and target. Does not send or enroll. "
    "Use a stable idempotency key; do not replace a held/unknown operation with another draft or key.",
    _prepare,
    {
        "conversation_id": _UUID,
        "social_account_id": _UUID,
        "platform": {"type": "string", "enum": ["facebook", "instagram_login"]},
        "target_message_id": _UUID,
        "expected_revision": _REVISION,
        "expected_generation": _REVISION,
        "expected_owner_epoch": _EPOCH,
        "body": {"type": "string", "minLength": 1, "maxLength": 20000},
        "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
    },
)
_register(
    "claim_conversation_reply",
    "Claim a due prepared V2 operation under current read/send permission and its enrolled owner. Returns its scoped "
    "claim and fencing token. A claim does not send, prove full provider freshness, or permit retry after unknown.",
    _claim,
    {"operation_id": _UUID},
)
_register(
    "dispatch_conversation_reply",
    "Send an explicitly owned, claimed V2 reply through the persistent DM gate. This is a real provider send when "
    "authorized and enabled. Requires current grants, owner epoch and claim fence. acknowledge_observed_state=true "
    "accepts the stated observed-history limitation; it is never proof that native/external activity is absent. "
    "An unknown result must not be retried, replaced, cleared by time or bypassed with the old send tool.",
    _dispatch,
    {
        "operation_id": _UUID,
        "claim_token": _UUID,
        "fencing_token": _EPOCH,
        "expected_owner_epoch": _EPOCH,
        "acknowledge_observed_state": {"type": "boolean"},
    },
)
_register(
    "get_conversation_reply_operation",
    "Read an owned V2 operation's status using current read/send permission. Never dispatches, clears uncertainty, "
    "or returns draft content, idempotency keys or stored claim tokens.",
    _get_operation,
    {"operation_id": _UUID},
)
