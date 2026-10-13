"""Saved All Types list shared by the UI and machine callers.

Source selection belongs to the existing unified reader. This service never
fetches native snapshots, writes read/work/draft state, or authorizes sends.
"""

import json
from typing import Literal
from uuid import UUID

from django.core.serializers.json import DjangoJSONEncoder
from django.http import QueryDict
from pydantic import BaseModel, ConfigDict, Field

from . import unified_feed


class SavedInboxListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["all", "dm", "comment", "mention", "review"] = "all"
    account: UUID | None = None
    platform: str = ""
    q: str = Field(default="", max_length=500)
    status: str = ""
    workflow: str = ""
    view: Literal["all", "mine", "unassigned"] = "all"
    cursor: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=30, ge=1, le=100)


def read_list(scope, parameters, *, limit=30):
    selected = unified_feed.filters(parameters)
    result = unified_feed.read_feed(scope, selected, cursor=parameters.get("cursor") or None, limit=limit)
    result["filters"] = selected
    return result


def list_parameters(values):
    """Convert validated machine arguments into the same filters as the UI."""
    parameters = QueryDict(mutable=True)
    for name, value in values.items():
        if value is not None and name != "limit":
            parameters[name] = str(value)
    return parameters


def list_payload(result):
    """Serialize the very same projected rows; keep the snapshot guard private."""
    result["guard"]()
    payload = json.loads(
        json.dumps(
            {key: result[key] for key in ("rows", "next_cursor", "filters", "unavailable_sources")},
            cls=DjangoJSONEncoder,
        )
    )
    result["guard"]()
    return payload


def resolve_target(scope, target_id):
    """Resolve a saved target inside current scope; caller never selects a source."""
    from . import canonical_reads as reader
    from .canonical_access import identifier as parse_identifier
    from .canonical_compat import selected_inbox_source
    from .models import InboxConversation, InboxMessage

    identifier = parse_identifier(target_id)
    allowed, _ = scope.refresh()
    conversations = InboxConversation.objects.filter(
        pk=identifier,
        workspace_id=scope.workspace_id,
        social_account__workspace_id=scope.workspace_id,
        social_account_id__in=allowed.values("pk"),
    )
    message = (
        InboxMessage.objects.select_related("social_account")
        .filter(
            pk=identifier,
            workspace_id=scope.workspace_id,
            social_account__workspace_id=scope.workspace_id,
            social_account_id__in=allowed.values("pk"),
        )
        .first()
    )
    conversation = conversations.first()
    if conversation is not None and message is not None:
        raise reader.CanonicalReadError("ambiguous_target", "This saved target has conflicting identities.")
    if conversation is not None:
        return "canonical", conversation.pk
    if message is None:
        raise reader.CanonicalReadError("not_found_or_denied", "This saved target is unavailable.")
    if message.message_type == "dm":
        selection = selected_inbox_source(scope, social_account_ids=[message.social_account_id])
        if selection["source"] == "canonical":
            return "canonical", reader.resolve_legacy_conversation(scope, message.pk)
    return "legacy", message


def read_detail(scope, target_id, *, cursor=None, limit=30):
    from . import canonical_reads as reader
    from .thread_reads import read_stored_thread

    kind, target = resolve_target(scope, target_id)
    if kind == "canonical":
        return {
            "kind": kind,
            "target_id": str(target),
            "data": reader.read_conversation(scope, target, cursor=cursor, limit=limit),
        }
    from .canonical_compat import inbox_source_snapshot

    _, snapshot = inbox_source_snapshot(scope, social_account_ids=[target.social_account_id])
    try:
        result = read_stored_thread(target, actor_id=scope.principal, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise reader.CanonicalReadError("invalid_cursor", str(exc)) from exc
    # A legacy reader does not have the canonical reader's final scope guard.
    # Recheck routing, grants and exact anchor identity before exposing it.
    again_kind, again = resolve_target(scope, target_id)
    if (
        again_kind != kind
        or again.pk != target.pk
        or again.social_account_id != target.social_account_id
        or inbox_source_snapshot(scope, social_account_ids=[target.social_account_id])[1] != snapshot
    ):
        raise reader.CanonicalReadError("stale_scope", "This saved target changed while reading.")
    return {"kind": kind, "target_id": str(target.pk), "data": result}
