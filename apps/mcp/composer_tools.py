"""Typed current-actor actions in one canonical conversation composer."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from apps.inbox.composer_surfaces import ConversationDraftInput, ConversationRetireInput, mutate_composer, read_composer
from apps.inbox.conversation_policy import read_available
from apps.mcp.handlers import _wrap_text
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tools import Tool, register_tool


class ComposerReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID
    adopt_reply_id: UUID | None = None
    observation_token: str | None = Field(default=None, max_length=4096, strict=True)


class ComposerDraftInput(ConversationDraftInput):
    conversation_id: UUID


class ComposerRetireInput(ConversationRetireInput):
    conversation_id: UUID


def _get_composer(args, context):
    try:
        value = ComposerReadInput.model_validate(args)
        return _wrap_text(read_composer(key=context["api_key"], request=context.get("request"), **value.model_dump()))
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from None


def _mutation(args, context, action):
    try:
        value = (ComposerRetireInput if action == "retire" else ComposerDraftInput).model_validate(args)
        return _wrap_text(
            mutate_composer(
                key=context["api_key"],
                request=context.get("request"),
                conversation_id=value.conversation_id,
                payload=value.model_dump(exclude={"conversation_id"}),
                action=action,
            )
        )
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from None


def _save_draft(args, context):
    return _mutation(args, context, "save")


def _send_reply(args, context):
    return _mutation(args, context, "send")


def _retire_reply(args, context):
    return _mutation(args, context, "retire")


for name, description, schema, handler in (
    (
        "get_inbox_conversation_composer",
        "Read the canonical conversation draft, revision and scope token. With canonical reads enabled, returns the actual bounded newest timeline and its observation_token for the next send. Reading never sends or adopts a historical draft.",
        ComposerReadInput,
        _get_composer,
    ),
    (
        "save_inbox_conversation_draft",
        "Save one conversation draft under its revision. Requires current inbox permission. quote_target_id selects an optional native quote; null clears it. No provider send.",
        ComposerDraftInput,
        _save_draft,
    ),
    (
        "send_inbox_conversation_reply",
        "Send an explicitly requested conversation message using current send permission and the automated 24-hour window. New message means a new action nonce; same nonce replays its receipt. Once attempted, body and quote cannot change. Unknown outcomes hold further sends. Existing owners require the observation_token returned with the newest timeline for this actor. A stale token requires reading the newest timeline again; never invent it or change sender ownership.",
        ComposerDraftInput,
        _send_reply,
    ),
    (
        "retire_inbox_conversation_reply",
        "Explicitly release an unattempted draft or verified-not-sent failed action. Preserve text, nonce and receipts. Never use to clear an unknown outcome; this operation does not send a successor.",
        ComposerRetireInput,
        _retire_reply,
    ),
):
    register_tool(
        Tool(
            name=name,
            description=description,
            input_schema=schema.model_json_schema(),
            handler=handler,
            enabled_setting="INBOX_CONVERSATION_COMPOSER_ENABLED",
            enabled_predicate=read_available,
        )
    )
