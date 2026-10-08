"""Bounded default-content continuation with current actor and source proof."""

from django.core import signing

from .canonical_access import digest, narrow_scope
from .canonical_content import visible_content
from .models import ConversationMessage, InboxConversation

BODY_SALT = "brightbean.canonical-body.v1"


def read_message_body(scope, message_id, *, cursor=None, limit=2000, unassigned=False):
    from .canonical_reads import (
        CanonicalReadError,
        _denied,
        _identity,
        _limit,
        _recheck,
        _scope_filter,
        _snapshot,
        _uuid,
    )

    _limit(limit, 4000)
    scope = narrow_scope(scope, target=(ConversationMessage, message_id))
    accounts, stamp = _snapshot(scope)
    row = ConversationMessage.objects.filter(
        _scope_filter(scope, accounts, messages=True, unassigned=unassigned), pk=_uuid(message_id)
    ).first()
    if row is None or (not unassigned and row.conversation_id is None):
        raise _denied()
    conversation = InboxConversation.objects.filter(_scope_filter(scope, accounts), pk=row.conversation_id).first()
    if conversation is None and not unassigned:
        raise _denied()
    content = visible_content(row)
    binding = [
        stamp,
        str(row.pk),
        [
            str(row.workspace_id),
            str(row.social_account_id),
            row.platform,
            row.platform_message_id,
            str(row.legacy_message_id),
            str(row.legacy_reply_id),
            row.direction,
            row.sender_id,
            row.recipient_id,
        ],
        _identity(conversation) if conversation else ["unassigned", str(row.social_account_id), row.platform],
        row.updated_at.isoformat(),
        digest(content),
        limit,
    ]
    offset = 0
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                raise ValueError
            value = signing.loads(cursor, salt=BODY_SALT, max_age=3600)
            offset = value["offset"]
            if (
                value["scope"] != binding
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or not 0 <= offset <= len(content["body"])
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, signing.BadSignature) as exc:
            raise CanonicalReadError("stale_cursor", "The content changed or its page expired; reload.") from exc
    body = content["body"][offset : offset + limit]
    end = offset + len(body)
    _recheck(scope, stamp)
    stable = (
        InboxConversation.objects.filter(
            _scope_filter(scope, accounts),
            pk=conversation.pk,
            revision=conversation.revision,
            workspace_id=conversation.workspace_id,
            social_account_id=conversation.social_account_id,
            platform=conversation.platform,
            platform_conversation_id=conversation.platform_conversation_id,
        ).exists()
        if conversation
        else ConversationMessage.objects.filter(
            _scope_filter(scope, accounts, messages=True, unassigned=True),
            pk=row.pk,
            updated_at=row.updated_at,
            social_account_id=row.social_account_id,
            platform=row.platform,
            platform_message_id=row.platform_message_id,
        ).exists()
    )
    if digest(visible_content(row)) != digest(content) or not stable:
        raise CanonicalReadError("stale_revision", "The content changed while reading; reload.")
    _recheck(scope, stamp)
    more = end < len(content["body"])
    return {
        "source": "canonical",
        "id": str(row.pk),
        "conversation_id": str(conversation.pk) if conversation else None,
        "body": body,
        "body_offset": offset,
        "has_more": more,
        "next_cursor": signing.dumps({"scope": binding, "offset": end}, salt=BODY_SALT) if more else None,
        "available": content["available"],
        "is_deleted": content["is_deleted"],
        "is_expired": content["is_expired"],
        "content_status": content["content_status"],
    }
