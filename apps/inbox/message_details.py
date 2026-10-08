"""Explicit, session-only bounded message reading. No provider or GET writes."""

from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.members.decorators import require_permission
from providers.meta_inbox_content import safe_attachment_url

from . import canonical_reads as reader
from .canonical_access import digest, narrow_scope
from .models import ConversationMessage, InboxConversation

SALT = "brightbean.internal-withdrawn-view.v1"


def _scoped(scope, conversation_id):
    accounts, stamp = reader._snapshot(scope)
    if not scope.principal.startswith("session:"):
        raise reader._denied()
    if conversation_id is None:
        from .unassigned_reads import _query

        return _query(scope, accounts), None, stamp
    conversation = InboxConversation.objects.filter(reader._scope_filter(scope, accounts), pk=conversation_id).first()
    if conversation is None:
        raise reader._denied()
    rows = ConversationMessage.objects.filter(
        reader._scope_filter(scope, accounts, messages=True),
        conversation=conversation,
        social_account_id=conversation.social_account_id,
        platform=conversation.platform,
    )
    return rows, conversation, stamp


def retained_capabilities(scope, conversation_id, identifiers):
    """Capability lookup selects identity metadata only; archive text never enters the page."""
    from .sync_observations import withdrawn_content_available_for_internal_review

    if conversation_id is not None:
        scope = narrow_scope(scope, target=(InboxConversation, conversation_id))
    rows, _, stamp = _scoped(scope, conversation_id)
    ids = {
        str(row.pk)
        for row in rows.filter(pk__in=identifiers).only(
            "pk", "workspace_id", "social_account_id", "platform", "platform_message_id", "content_status"
        )
        if withdrawn_content_available_for_internal_review(row)
    }
    reader._recheck(scope, stamp)
    return ids


def _retained_value(scope, conversation_id, message_id):
    from .sync_observations import withdrawn_content_for_internal_review

    if conversation_id is not None:
        scope = narrow_scope(scope, target=(InboxConversation, conversation_id))
    rows, conversation, stamp = _scoped(scope, conversation_id)
    row = rows.select_related("observation_state").filter(pk=message_id).first()
    if row is None:
        raise reader._denied()
    content = withdrawn_content_for_internal_review(row)
    if not content or not (content["body"] or content["attachments"]):
        raise reader._denied()
    state = row.observation_state
    body = content["body"]
    items = content["attachments"] if isinstance(content["attachments"], list) else []
    binding = digest(
        [
            stamp,
            reader._identity(conversation) if conversation else None,
            str(row.pk),
            row.platform_message_id,
            row.sender_id,
            row.recipient_id,
            str(row.legacy_message_id),
            str(row.legacy_reply_id),
            str(state.connection_generation),
            state.withdrawn_at,
            state.updated_at,
            body,
            items,
        ]
    )
    reader._recheck(scope, stamp)
    return body, items, binding


def read_retained(scope, conversation_id, message_id, cursor=None):
    body, items, binding = _retained_value(scope, conversation_id, message_id)
    body_offset = media_offset = 0
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError
            value = signing.loads(cursor, salt=SALT, max_age=3600)
            body_offset, media_offset = value["body"], value["media"]
            if (
                value["scope"] != binding
                or type(body_offset) is not int
                or type(media_offset) is not int
                or not 0 <= body_offset <= len(body)
                or not 0 <= media_offset <= len(items)
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError, signing.BadSignature) as exc:
            raise reader.CanonicalReadError("stale_cursor", "Content changed. Reopen it.") from exc
    text = body[body_offset : body_offset + 2000]
    media = []
    for item in items[media_offset : media_offset + 3]:
        if isinstance(item, dict):
            media.append(
                {
                    "title": str(item.get("title", ""))[:500],
                    "type": str(item.get("type", "attachment"))[:30],
                    "url": safe_attachment_url(item.get("url")),
                }
            )
    end_body, end_media = body_offset + len(text), min(len(items), media_offset + 3)
    more = end_body < len(body) or end_media < len(items)
    if _retained_value(scope, conversation_id, message_id)[2] != binding:
        raise reader.CanonicalReadError("stale_cursor", "Content changed. Reopen it.")
    return {
        "source": "canonical",
        "id": str(message_id),
        "conversation_id": str(conversation_id) if conversation_id else None,
        "body": text,
        "items": media,
        "is_deleted": True,
        "available": True,
        "has_more": more,
        "next_cursor": signing.dumps({"scope": binding, "body": end_body, "media": end_media}, salt=SALT)
        if more
        else None,
    }


@login_required
@require_permission("use_inbox")
@require_POST
@never_cache
def detail(request, workspace_id, message_id, conversation_id=None):
    from .canonical_views import _error
    from .views import _get_workspace

    workspace = _get_workspace(request, workspace_id)
    if not reader.enabled():
        return HttpResponse("Not found.", status=404)
    kind, cursor = request.POST.get("kind"), request.POST.get("cursor") or None
    try:
        scope = narrow_scope(
            reader.session_read_scope(request.user, workspace.pk), target=(ConversationMessage, message_id)
        )
        rows, _, stamp = _scoped(scope, conversation_id)
        if not rows.filter(pk=message_id).exists():
            raise reader._denied()
        if kind == "retained":
            result = read_retained(scope, conversation_id, message_id, cursor)
        elif kind == "body":
            result = (reader.read_message_body if conversation_id else reader.read_unassigned_message_body)(
                scope, message_id, cursor=cursor
            )
        elif kind == "attachments":
            result = (
                reader.read_message_attachments if conversation_id else reader.read_unassigned_message_attachments
            )(scope, message_id, cursor=cursor)
        else:
            return HttpResponse("Unknown message content.", status=422)
        fresh, _, _ = _scoped(scope, conversation_id)
        if not fresh.filter(pk=message_id).exists():
            raise reader._denied()
        reader._recheck(scope, stamp)
        result.update(id=str(message_id), conversation_id=str(conversation_id) if conversation_id else None, kind=kind)
    except reader.CanonicalReadError as exc:
        return _error(exc)
    response = JsonResponse(result)
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, noarchive"
    return response
