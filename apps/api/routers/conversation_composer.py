"""Canonical conversation composer API; all sends use the shared receipt gate."""

from uuid import UUID

from django.http import JsonResponse
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.inbox.composer_surfaces import (
    ConversationDraftInput,
    ConversationRetireInput,
    mutate_composer,
    read_composer,
)

router = Router(tags=["inbox"])


class DraftRequest(ConversationDraftInput, Schema):
    pass


class RetireRequest(ConversationRetireInput, Schema):
    pass


def _response(value):
    response = JsonResponse(value)
    response["Cache-Control"] = "private, no-store"
    return response


@router.get("/{conversation_id}/composer")
def get_composer(
    request, conversation_id: UUID, adopt_reply_id: UUID | None = None, observation_token: str | None = None
):
    enforce_http_rate_limits(request, is_write=False)
    try:
        return _response(
            read_composer(
                key=request.api_key,
                conversation_id=conversation_id,
                request=request,
                adopt_reply_id=adopt_reply_id,
                observation_token=observation_token,
            )
        )
    except ValueError as exc:
        raise HttpError(409, str(exc)) from None


def _mutate(request, conversation_id, payload, action):
    enforce_http_rate_limits(request, is_write=True)
    try:
        result = mutate_composer(
            key=request.api_key,
            conversation_id=conversation_id,
            payload=payload.model_dump(),
            action=action,
            request=request,
        )
    except ValueError as exc:
        raise HttpError(409, str(exc)) from None
    log_audit_entry(request, action=f"inbox.conversation.{action}", target_id=result["id"], status_code=200)
    return _response(result)


@router.post("/{conversation_id}/draft")
def save_draft(request, conversation_id: UUID, payload: DraftRequest):
    return _mutate(request, conversation_id, payload, "save")


@router.post("/{conversation_id}/send")
def send_message(request, conversation_id: UUID, payload: DraftRequest):
    return _mutate(request, conversation_id, payload, "send")


@router.post("/{conversation_id}/retire")
def retire_action(request, conversation_id: UUID, payload: RetireRequest):
    return _mutate(request, conversation_id, payload, "retire")
