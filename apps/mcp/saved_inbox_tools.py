"""Intent list adapter over the UI's saved inbox application service."""

from apps.inbox import saved_reads
from apps.inbox.canonical_reads import enabled
from apps.inbox.saved_reads import SavedInboxListInput
from apps.mcp.canonical_reads import invoke, scope
from apps.mcp.handlers import _require_perm, _wrap_text
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tools import Tool, register_tool


def list_inbox(args, context):
    _require_perm(context, "use_inbox")
    try:
        values = SavedInboxListInput.model_validate(args)
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from None
    result = invoke(
        saved_reads.read_list,
        scope(context),
        saved_reads.list_parameters(values.model_dump()),
        limit=values.limit,
    )
    return _wrap_text(invoke(saved_reads.list_payload, result))


register_tool(
    Tool(
        name="list_inbox",
        description="List the same saved All Types inbox as the UI: conversations and separate public threads. Optionally filter by account, platform, type or search. The server selects saved sources; reading never fetches the platform, marks read or sends. Existing inbox tools remain available.",
        input_schema=SavedInboxListInput.model_json_schema(),
        handler=list_inbox,
        enabled_predicate=enabled,
    )
)


from apps.inbox.inbox_intents import (  # noqa: E402
    InboxActionInput,
    InboxReadInput,
    InboxStatusInput,
    inbox_send_status,
    mutate_inbox,
    read_inbox,
)


def _call_intent(schema, function, args, context, **extra):
    _require_perm(context, "use_inbox")
    if extra.get("action") == "send":
        _require_perm(context, "reply_from_inbox")
    try:
        value = schema.model_validate(args)
        return _wrap_text(
            invoke(function, key=context["api_key"], request=context.get("request"), **extra, **value.model_dump())
        )
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc), {"error": getattr(exc, "code", "invalid_action")}) from None


def _read_intent(args, context):
    return _call_intent(InboxReadInput, read_inbox, args, context)


def _action_intent(args, context, action):
    return _call_intent(InboxActionInput, mutate_inbox, args, context, action=action)


def _status_intent(args, context):
    return _call_intent(InboxStatusInput, inbox_send_status, args, context)


def _save_intent(args, context):
    return _action_intent(args, context, "save")


def _send_intent(args, context):
    return _action_intent(args, context, "send")


for name, schema, handler, description in (
    (
        "read_inbox",
        InboxReadInput,
        _read_intent,
        "Read a saved inbox target from list_inbox or an existing ID; server resolves sources. Newest saved history supplies one opaque action reference. No provider fetch or read acknowledgement.",
    ),
    (
        "save_inbox_draft",
        InboxActionInput,
        _save_intent,
        "Save a saved inbox draft using the action reference returned by read_inbox. Retain the returned reference. Saving never renews the observed history proof or sends.",
    ),
    (
        "send_inbox_reply",
        InboxActionInput,
        _send_intent,
        "Send an explicitly requested saved inbox reply using its saved action reference. The same reference is the same action; unknown outcomes cannot be retried. Stale context requires read_inbox. Existing ownership and automated reply-window checks apply.",
    ),
    (
        "get_inbox_send_status",
        InboxStatusInput,
        _status_intent,
        "Read the original action receipt. Provider acceptance is not delivery or read. Does not send, discard, retire or change workflow.",
    ),
):
    register_tool(
        Tool(
            name=name,
            description=description,
            input_schema=schema.model_json_schema(),
            handler=handler,
            enabled_predicate=enabled,
        )
    )
