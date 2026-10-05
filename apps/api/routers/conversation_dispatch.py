"""Opt-in conversation dispatch. Enrollment and owner control stay server-only."""

from typing import Literal
from uuid import UUID

from django.core.exceptions import PermissionDenied
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import ConfigDict, Field, StrictBool, StrictInt

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.inbox import reply_coordination, reply_dispatch
from apps.inbox.dispatch_access import (
    dispatch_actor,
    operation_result,
    require_request_permissions,
    visible_operation,
)
from apps.inbox.services import ReplyStateError

router = Router(tags=["conversation-dispatch"])


class PrepareConversationReply(Schema):
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID
    social_account_id: UUID
    platform: Literal["facebook", "instagram_login"]
    target_message_id: UUID
    expected_revision: StrictInt = Field(ge=0)
    expected_generation: StrictInt = Field(ge=0)
    expected_owner_epoch: StrictInt = Field(ge=1)
    body: str = Field(min_length=1, max_length=20000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class DispatchConversationReply(Schema):
    model_config = ConfigDict(extra="forbid")
    claim_token: UUID
    fencing_token: StrictInt = Field(ge=1)
    expected_owner_epoch: StrictInt = Field(ge=1)
    acknowledge_observed_state: StrictBool


def _access(request):
    if not reply_dispatch.dispatch_enabled():
        raise HttpError(404, "Conversation dispatch is not enabled")
    try:
        require_request_permissions(getattr(request, "workspace_membership", None))
    except PermissionDenied as exc:
        raise HttpError(403, str(exc)) from exc
    return dispatch_actor(request.api_key, request)


def _conflict(exc):
    # The service returns stable diagnostics; never serialize provider errors.
    code = getattr(exc, "code", "dispatch_held")
    return HttpError(409, f"Conversation reply held ({code})")


@router.post("/prepare", response=dict)
def prepare(request, payload: PrepareConversationReply):
    enforce_http_rate_limits(request, is_write=True)
    scope, authorization = _access(request)
    try:
        operation = reply_dispatch.prepare_owned_reply(scope, authorization=authorization, **payload.model_dump())
        result = operation_result(operation)
    except (reply_coordination.ReplyCoordinationError, ReplyStateError) as exc:
        raise _conflict(exc) from exc
    log_audit_entry(request, action="conversation_reply.prepare", target_id=operation.pk, status_code=200)
    return result


@router.post("/{operation_id}/claim", response=dict)
def claim(request, operation_id: UUID):
    enforce_http_rate_limits(request, is_write=True)
    scope, authorization = _access(request)
    try:
        visible_operation(scope, operation_id, authorization)
        operation = reply_dispatch.claim_owned_reply(scope, operation_id=operation_id, authorization=authorization)
        result = operation_result(operation, include_claim=True)
    except (reply_coordination.ReplyCoordinationError, ReplyStateError) as exc:
        raise _conflict(exc) from exc
    log_audit_entry(request, action="conversation_reply.claim", target_id=operation.pk, status_code=200)
    return result


@router.post("/{operation_id}/dispatch", response=dict)
def dispatch(request, operation_id: UUID, payload: DispatchConversationReply):
    enforce_http_rate_limits(request, is_write=True)
    scope, authorization = _access(request)
    try:
        visible_operation(scope, operation_id, authorization)
        operation = reply_dispatch.dispatch_reply(
            scope,
            operation_id=operation_id,
            authorization=authorization,
            actor=request.api_key.issued_by,
            **payload.model_dump(),
        )
        result = operation_result(operation)
    except (reply_coordination.ReplyCoordinationError, ReplyStateError) as exc:
        raise _conflict(exc) from exc
    log_audit_entry(request, action="conversation_reply.dispatch", target_id=operation.pk, status_code=200)
    return result


@router.get("/{operation_id}", response=dict)
def get_operation(request, operation_id: UUID):
    enforce_http_rate_limits(request, is_write=False)
    scope, authorization = _access(request)
    try:
        return operation_result(visible_operation(scope, operation_id, authorization))
    except (reply_coordination.ReplyCoordinationError, ReplyStateError) as exc:
        raise _conflict(exc) from exc
