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


def resolve_read_reference(scope, reference):
    """Resolve one scoped saved UUID or native ID; never probe a provider."""
    from django.db.models import Q

    from .canonical_reads import CanonicalReadError
    from .models import ConversationMessage, InboxConversation, InboxMessage
    from .presentation import stored_thread_messages

    text = str(reference)
    if not text or len(text) > 1024 or any(ord(c) < 33 or ord(c) == 127 for c in text):
        raise CanonicalReadError("invalid_reference", "Use one saved conversation or message ID.")
    allowed, _ = scope.refresh()
    bounds = {
        "workspace_id": scope.workspace_id,
        "social_account__workspace_id": scope.workspace_id,
        "social_account_id__in": allowed.values("pk"),
    }
    try:
        identifier = UUID(text)
    except ValueError:
        identifier = None
    candidates = {}
    sources = {}

    def add(target_id):
        kind, target = resolve_target(scope, target_id)
        if kind == "canonical":
            group = (kind, str(target))
        else:
            anchor = stored_thread_messages(target).order_by("created_at", "pk").values_list("pk", flat=True).first()
            group = (kind, str(target.social_account_id), str(anchor))
        candidates.setdefault(group, target_id)

    conv_filter = Q(platform_conversation_id=text)
    canonical_filter = Q(platform_message_id=text)
    legacy_filter = Q(platform_message_id=text)
    if identifier:
        conv_filter |= Q(pk=identifier)
        canonical_filter |= Q(pk=identifier)
        legacy_filter |= Q(pk=identifier)
    for model, query, field in (
        (InboxConversation, conv_filter, "pk"),
        (ConversationMessage, canonical_filter & Q(conversation__isnull=False), "conversation_id"),
        (InboxMessage, legacy_filter, "pk"),
    ):
        matches = list(
            model.objects.filter(query, **bounds).order_by().values("pk", "social_account_id", field).distinct()[:51]
        )
        if len(matches) > 50:
            raise CanonicalReadError(
                "ambiguous_reference", "This reference matches too many saved targets. Use its saved UUID."
            )
        for value in matches:
            if model is not InboxMessage and value["pk"] != identifier:
                from .canonical_compat import selected_inbox_source

                account_id = value["social_account_id"]
                if account_id not in sources:
                    sources[account_id] = selected_inbox_source(scope, social_account_ids=[account_id])["source"]
                if sources[account_id] != "canonical":
                    continue
            add(value[field])
    # A native legacy DM thread can contain many messages. Resolve once per
    # account, rather than requiring callers to enumerate or select accounts.
    legacy_threads = InboxMessage.objects.filter(message_type="dm", extra__conversation_id=text, **bounds)
    accounts = list(legacy_threads.order_by().values_list("social_account_id", flat=True).distinct()[:3])
    for account_id in accounts:
        add(
            legacy_threads.filter(social_account_id=account_id)
            .order_by("created_at", "pk")
            .values_list("pk", flat=True)
            .first()
        )
    if len(candidates) > 1 or len(accounts) > 2:
        raise CanonicalReadError(
            "ambiguous_reference",
            "This ID matches multiple saved accounts or threads. Use the exact saved UUID from list_inbox.",
        )
    if not candidates:
        raise CanonicalReadError("not_found_or_denied", "This saved conversation or message is unavailable.")
    return next(iter(candidates.values()))
