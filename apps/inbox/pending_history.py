"""Bounded, recoverable draft/receipt history from the existing reply ledger."""

from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID

from django.core import signing
from django.db.models import Q
from django.urls import reverse

from . import canonical_reads as reader
from .models import InboxReply

SALT = "brightbean.pending-conversation-history.v1"


def page(request, workspace, conversation, *, active=None, cursor=None):
    from .canonical_views import _reply_content

    scope = reader.narrow_scope(
        reader.session_read_scope(request.user, workspace.pk), social_account_ids=[conversation.social_account_id]
    )
    _, guard = reader._snapshot(scope)
    binding = [guard, str(conversation.pk), reader._identity(conversation), str(active.pk) if active else ""]
    queryset = (
        InboxReply.objects.filter(
            Q(conversation=conversation) | Q(inbox_message__conversation_message__conversation=conversation),
            inbox_message__workspace=workspace,
            inbox_message__social_account_id=conversation.social_account_id,
            inbox_message__social_account__workspace=workspace,
        )
        .exclude(status="sent")
        .select_related("inbox_message__social_account", "author")
    )
    if active:
        queryset = queryset.exclude(pk=active.pk)
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError
            data = signing.loads(cursor, salt=SALT, max_age=3600)
            if data["scope"] != binding:
                raise ValueError
            created, identifier = datetime.fromisoformat(data["position"][0]), UUID(data["position"][1])
        except (ValueError, TypeError, KeyError, IndexError, signing.BadSignature) as exc:
            raise reader.CanonicalReadError("stale_cursor", "Draft history changed. Open its first page.") from exc
        queryset = queryset.filter(Q(created_at__lt=created) | Q(created_at=created, pk__lt=identifier))
    candidates = list(queryset.order_by("-created_at", "-pk")[:11])
    rows = candidates[:10]
    for reply in rows:
        _reply_content(reply)
        reply.can_adopt = bool(
            reply.conversation_id is None
            and reply.status == "draft"
            and reply.content_available
            and not reply.send_generation
            and not reply.dm_send_attempts.exists()
            and not hasattr(reply, "send_operation")
            and not reply.is_follow_up
            and not reply.follow_up_of_id
            and not reply.retired_at
        )
    newest = reverse(
        "inbox:conversation_pending_history",
        kwargs={
            "workspace_id": workspace.pk,
            "conversation_id": conversation.pk,
        },
    )
    params = {}
    if active is not None and active.conversation_id is None and request.GET.get("adopt_reply_id") == str(active.pk):
        params["adopt_reply_id"] = str(active.pk)
    first_url = newest + "?" + urlencode(params) if params else newest
    next_url = ""
    if len(candidates) > 10:
        token = signing.dumps(
            {"scope": binding, "position": [rows[-1].created_at.isoformat(), str(rows[-1].pk)]}, salt=SALT
        )
        next_url = newest + "?" + urlencode({**params, "cursor": token})
    reader._recheck(scope, guard)
    return {"pending_replies": rows, "pending_next_url": next_url, "pending_first_url": first_url if cursor else ""}
