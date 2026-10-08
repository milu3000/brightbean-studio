"""MCP adapters over the same canonical read policy as the session UI."""

from apps.inbox import canonical_reads as reader
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError


def scope(context):
    return reader.key_read_scope(context["api_key"], context.get("request"))


def invoke(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except reader.CanonicalReadError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc), {"error": exc.code, **(exc.data or {})}) from exc


def list_conversations(args, context):
    return invoke(
        reader.list_conversations,
        scope(context),
        **{
            name: args[name]
            for name in ("social_account_id", "platform", "workflow_state", "search", "cursor", "limit")
            if name in args
        },
    )


def messages(args, context):
    if args.get("unassigned_only") is True and not args.get("conversation_id"):
        if not args.get("social_account_id"):
            raise JsonRpcError(INVALID_PARAMS, "Unassigned history requires social_account_id.")
        return invoke(
            reader.list_unassigned_messages,
            scope(context),
            social_account_id=args["social_account_id"],
            cursor=args.get("cursor"),
            limit=args.get("limit", 30),
        )
    if not args.get("conversation_id") or "social_account_id" in args or "unassigned_only" in args:
        raise JsonRpcError(
            INVALID_PARAMS, "Canonical history requires an actual conversation_id; unattributed rows are held."
        )
    result = invoke(
        reader.read_conversation,
        scope(context),
        args["conversation_id"],
        cursor=args.get("cursor"),
        limit=args.get("limit", 30),
    )
    # Tool reads never acknowledge or furnish an automatic acknowledgement action.
    result.pop("read_ack_token", None)
    return result


def reply_context(args, context):
    actor = scope(context)
    target = args.get("message_id")
    try:
        conversation_id = reader.resolve_legacy_conversation(actor, target)
    except reader.CanonicalReadError:
        incoming = invoke(reader.read_canonical_incoming_message, actor, target)
        conversation_id = incoming["canonical_conversation_id"]
    result = invoke(reader.read_conversation, actor, conversation_id, limit=args.get("limit", 20))
    result.pop("read_ack_token", None)
    result.update(message_id=str(target), send_preconditions_enforced=False, history_tool="get_conversation_messages")
    return result


def attachments(args, context):
    return invoke(
        reader.read_message_attachments,
        scope(context),
        args.get("message_id"),
        cursor=args.get("cursor"),
        limit=args.get("limit", 3),
    )
