"""Machine adapter for the same saved All Types list rendered by the UI."""

from uuid import UUID

from ninja import Query, Router, Schema
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.inbox import saved_reads
from apps.inbox.saved_reads import SavedInboxListInput

from .canonical_inbox import invoke, read_scope, respond

router = Router(tags=["inbox"])


class SavedInboxFilters(SavedInboxListInput, Schema):
    pass


@router.get("/", response=dict, summary="List saved inbox conversations and public threads")
def list_inbox(request, filters: SavedInboxFilters = Query(...)):  # noqa: B008 - Ninja query schema registration
    from .inbox import _require_perm

    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "use_inbox")
    result = invoke(
        saved_reads.read_list,
        read_scope(request),
        saved_reads.list_parameters(filters.model_dump()),
        limit=filters.limit,
    )
    return respond(request, invoke(saved_reads.list_payload, result), "inbox.saved.list")


from apps.inbox.inbox_intents import (  # noqa: E402
    InboxActionInput,
    InboxStatusInput,
    inbox_send_status,
    mutate_inbox,
    read_inbox,
)


class InboxActionRequest(InboxActionInput, Schema):
    pass


class InboxStatusRequest(InboxStatusInput, Schema):
    pass


@router.get("/targets/{target_id}", response=dict, summary="Read a saved inbox target without choosing a source")
def read_target(request, target_id: UUID, cursor: str | None = None, limit: int = Query(30, ge=1, le=50)):
    from .inbox import _require_perm

    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "use_inbox")
    result = invoke(read_inbox, key=request.api_key, request=request, target_id=target_id, cursor=cursor, limit=limit)
    return respond(request, result, "inbox.saved.read", str(target_id))


def _intent(request, payload, action):
    from .inbox import _require_perm

    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "use_inbox")
    if action == "send":
        _require_perm(request, "reply_from_inbox")
    try:
        result = invoke(mutate_inbox, key=request.api_key, request=request, action=action, **payload.model_dump())
    except ValueError as exc:
        raise HttpError(409, str(exc)) from None
    return respond(request, result, "inbox.saved." + action)


@router.post("/draft", response=dict)
def save_draft_intent(request, payload: InboxActionRequest):
    return _intent(request, payload, "save")


@router.post("/send", response=dict)
def send_reply_intent(request, payload: InboxActionRequest):
    return _intent(request, payload, "send")


@router.post("/status", response=dict)
def read_send_status(request, payload: InboxStatusRequest):
    from .inbox import _require_perm

    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "use_inbox")
    try:
        result = invoke(inbox_send_status, key=request.api_key, request=request, **payload.model_dump())
    except ValueError as exc:
        raise HttpError(409, str(exc)) from None
    return respond(request, result, "inbox.saved.status")
