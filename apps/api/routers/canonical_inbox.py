"""Saved canonical inbox reads; no provider calls or implicit read acknowledgement."""

from uuid import UUID

from django.http import JsonResponse
from ninja import Query, Router, Schema
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.inbox import canonical_reads as reader

router = Router(tags=["inbox-conversations"])


def read_scope(request):
    return reader.key_read_scope(request.api_key, request)


def invoke(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except reader.CanonicalReadError as exc:
        status = 422 if exc.code.startswith("invalid_") else 404 if exc.code == "not_found_or_denied" else 409
        error = HttpError(status, str(exc))
        error.canonical_data = {"error": exc.code, **(exc.data or {})}
        raise error from exc


def respond(request, result, action, target_id=None):
    log_audit_entry(request, action=action, target_id=target_id, status_code=200)
    response = JsonResponse(result)
    response["Cache-Control"] = "private, no-store"
    return response


@router.get("/", response=dict, summary="List saved bidirectional DM conversations")
def list_conversations(
    request,
    social_account_id: UUID | None = None,
    platform: str | None = None,
    workflow_state: str | None = None,
    search: str = "",
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
):
    enforce_http_rate_limits(request, is_write=False)
    value = invoke(
        reader.list_conversations,
        read_scope(request),
        social_account_id=social_account_id,
        platform=platform,
        workflow_state=workflow_state,
        search=search,
        cursor=cursor,
        limit=limit,
    )
    return respond(request, value, "inbox.conversations.list")


@router.get("/unassigned", response=dict, summary="List verified saved messages without a native conversation")
def list_unassigned(
    request,
    social_account_id: UUID | None = None,
    platform: str | None = None,
    search: str = "",
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
):
    enforce_http_rate_limits(request, is_write=False)
    value = invoke(
        reader.list_unassigned_messages,
        read_scope(request),
        social_account_id=social_account_id,
        platform=platform,
        search=search,
        cursor=cursor,
        limit=limit,
    )
    return respond(request, value, "inbox.unassigned.list")


@router.get("/unassigned/{message_id}", response=dict, summary="Read an actual unassigned canonical message")
def get_unassigned(request, message_id: UUID):
    enforce_http_rate_limits(request, is_write=False)
    return respond(
        request,
        invoke(reader.read_unassigned_message, read_scope(request), message_id),
        "inbox.unassigned.read",
        message_id,
    )


@router.get("/unassigned/{message_id}/body", response=dict, summary="Continue an unassigned message body")
def get_unassigned_body(request, message_id: UUID, cursor: str | None = None, limit: int = Query(2000, ge=1, le=4000)):
    enforce_http_rate_limits(request, is_write=False)
    return respond(
        request,
        invoke(reader.read_unassigned_message_body, read_scope(request), message_id, cursor=cursor, limit=limit),
        "inbox.unassigned.body",
        message_id,
    )


@router.get("/unassigned/{message_id}/attachments", response=dict, summary="Continue unassigned attachment metadata")
def get_unassigned_attachments(
    request, message_id: UUID, cursor: str | None = None, limit: int = Query(3, ge=1, le=10)
):
    enforce_http_rate_limits(request, is_write=False)
    return respond(
        request,
        invoke(reader.read_unassigned_message_attachments, read_scope(request), message_id, cursor=cursor, limit=limit),
        "inbox.unassigned.attachments",
        message_id,
    )


@router.get("/{conversation_id}", response=dict, summary="Read saved canonical DM history")
def get_conversation(request, conversation_id: UUID, cursor: str | None = None, limit: int = Query(30, ge=1, le=100)):
    enforce_http_rate_limits(request, is_write=False)
    value = invoke(reader.read_conversation, read_scope(request), conversation_id, cursor=cursor, limit=limit)
    return respond(request, value, "inbox.conversations.read", conversation_id)


class ReadAcknowledgement(Schema):
    read_ack_token: str


@router.post("/{conversation_id}/read", response=dict, summary="Acknowledge an explicitly displayed page")
def acknowledge_read(request, conversation_id: UUID, payload: ReadAcknowledgement):
    enforce_http_rate_limits(request, is_write=True)
    value = invoke(reader.acknowledge_read, read_scope(request), conversation_id, payload.read_ack_token)
    return respond(request, value, "inbox.conversations.read_ack", conversation_id)
