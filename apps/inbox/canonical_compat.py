"""Explicit legacy read contracts without synthetic incoming records or sends."""

from copy import copy
from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse

from .canonical_access import archive_identity, enabled, narrow_scope
from .canonical_content import visible_content
from .conversation_policy import read_allowed
from .models import ConversationMessage, InboxConversation, InboxMessage, InboxReply


def inbox_source_snapshot(scope, *, social_account_ids=None):
    """Fresh account-scoped read entry points shared by session, REST and MCP.

    Held accounts remain canonical-owned. Discovery is metadata only and never
    grants permission to read their legacy shadows.
    """
    from .canonical_access import CanonicalReadError, denied, digest, identifier
    from .sync_identity import canonical_owns_account

    selected = {identifier(value) for value in social_account_ids} if social_account_ids is not None else None

    def snapshot():
        allowed, grants = scope.refresh()
        accounts = allowed.filter(workspace_id=scope.workspace_id)
        if selected is not None:
            accounts = accounts.filter(pk__in=selected)
        accounts = list(
            accounts.only(
                "pk",
                "workspace_id",
                "platform",
                "account_platform_id",
                "webhook_target_id",
                "account_name",
                "account_handle",
                "connection_status",
            ).order_by("pk")
        )
        if selected is not None and {account.pk for account in accounts} != selected:
            raise denied()
        result = []
        for account in accounts:
            readable = enabled() and (read_allowed(account) or archive_identity(account) is not None)
            source = "canonical" if readable else "canonical_held" if canonical_owns_account(account) else "legacy"
            account_id = str(account.pk)
            canonical = source != "legacy"
            arguments = {"social_account_id": account_id}
            if not canonical:
                arguments["message_type"] = "dm"
            result.append(
                {
                    "id": account_id,
                    "platform": account.platform,
                    "account_name": account.account_name,
                    "account_handle": account.account_handle,
                    "source": source,
                    "inbox_url": reverse("inbox:feed", kwargs={"workspace_id": scope.workspace_id})
                    + "?"
                    + urlencode({"domain": "dm", "account": account_id}),
                    "api": ("/api/v1/inbox-conversations/" if canonical else "/api/v1/inbox/")
                    + "?"
                    + urlencode(arguments),
                    "tool": "list_conversations" if canonical else "list_inbox_messages",
                    "arguments": arguments,
                }
            )
        return result, digest(
            [
                grants,
                result,
                [
                    [
                        account.pk,
                        account.workspace_id,
                        account.account_platform_id,
                        account.webhook_target_id,
                        account.connection_status,
                    ]
                    for account in accounts
                ],
            ]
        )

    sources, stamp = snapshot()
    if snapshot()[1] != stamp:
        raise CanonicalReadError("stale_scope", "The current inbox grants or account scope changed; reload.")
    return sources, stamp


def inbox_sources(scope, *, social_account_ids=None):
    return inbox_source_snapshot(scope, social_account_ids=social_account_ids)[0]


def selected_inbox_source(scope, *, social_account_ids=None, platforms=None):
    from .canonical_access import CanonicalReadError

    sources, stamp = inbox_source_snapshot(scope, social_account_ids=social_account_ids)
    if platforms:
        sources = [item for item in sources if item["platform"] in platforms]
    if any(item["source"] == "canonical_held" for item in sources):
        raise CanonicalReadError(
            "canonical_unavailable",
            "This account requires the saved canonical inbox; legacy fallback is held.",
            data={"account_sources": sources},
        )
    return {
        "source": "canonical" if any(item["source"] == "canonical" for item in sources) else "legacy",
        "accounts": sources,
        "stamp": stamp,
    }


def recheck_inbox_source(scope, expected, *, social_account_ids=None, platforms=None):
    from .canonical_access import CanonicalReadError

    if (
        expected is not None
        and selected_inbox_source(scope, social_account_ids=social_account_ids, platforms=platforms) != expected
    ):
        raise CanonicalReadError("stale_scope", "The current inbox source or grants changed; reload.")


def hold_legacy_fallback(scope, *, social_account_id=None):
    selected_inbox_source(scope, social_account_ids=[social_account_id] if social_account_id is not None else None)


def _upgrade(*, conversation_id=None, social_account_id=None, sources=None):
    from .canonical_reads import CanonicalReadError

    uri = f"/api/v1/inbox-conversations/{conversation_id}" if conversation_id else "/api/v1/inbox-conversations/"
    if social_account_id and not conversation_id:
        uri += f"?social_account_id={social_account_id}"
    return CanonicalReadError(
        "canonical_upgrade_required",
        f"Use canonical inbox contract v2 at {uri}.",
        data={
            "contract_version": 2,
            "canonical_api": uri,
            "canonical_tool": "get_conversation_messages" if conversation_id else "list_conversations",
            "conversation_id": str(conversation_id) if conversation_id else None,
            "coverage": "bidirectional_saved_dm",
            "history_complete": False,
            "legacy_public_message_types": ["comment", "mention", "review"],
            **({"account_sources": sources} if sources is not None else {}),
        },
    )


def _is_canonical(scope, message):
    if message.message_type != "dm":
        return False
    selection = selected_inbox_source(scope, social_account_ids=[message.social_account_id])
    if selection["source"] == "canonical":
        return True
    # Even a disabled presentation must not return a blank transport adapter.
    if isinstance(message.extra, dict) and message.extra.get("transport_projection") is True:
        raise _upgrade()
    return False


def list_legacy_dm_adapter(scope, *, message_type=None, status=None, social_account_id=None, cursor=None, limit=30):
    if message_type not in {None, "dm"}:
        return None
    selection = selected_inbox_source(
        scope, social_account_ids=[social_account_id] if social_account_id is not None else None
    )
    if selection["source"] == "canonical":
        raise _upgrade(social_account_id=social_account_id, sources=selection["accounts"])
    return selection


def read_legacy_thread(scope, message, *, cursor=None, limit=30):
    from .canonical_reads import resolve_legacy_conversation

    if not _is_canonical(scope, message):
        return None
    raise _upgrade(conversation_id=resolve_legacy_conversation(scope, message.pk))


def _receipt(reply):
    from apps.api.schemas import InboxReplyResponse

    value = InboxReplyResponse.from_reply(reply).model_dump(mode="json")
    row = ConversationMessage.objects.filter(legacy_reply=reply).first()
    if row is not None:
        content = visible_content(row)
        value["body"] = content["body"]
        if not content["available"]:
            value["send_error"] = ""
    elif getattr(reply, "content_compacted_at", None) or (
        getattr(reply, "conversation_id", None) and reply.status == "sent"
    ):
        value["body"], value["send_error"] = "", ""
    return value


def _metadata(scope, conversation):
    from .canonical_reads import _classification, _read_state

    return {
        "source": "canonical",
        **_classification(conversation),
        "canonical_contract_version": 2,
        "canonical_conversation_id": str(conversation.pk),
        "canonical_api": f"/api/v1/inbox-conversations/{conversation.pk}",
        "canonical_tool": "get_conversation_messages",
        "canonical_coverage": {"history_complete": False, "native_outgoing_in_replies": False},
        "canonical_workflow_state": conversation.workflow_state,
        "canonical_read_state": _read_state(conversation, scope.user_id),
    }


def _stable(scope, accounts, token, row, conversation):
    from .canonical_reads import CanonicalReadError, _recheck, _scope_filter

    _recheck(scope, token)
    if (
        not ConversationMessage.objects.filter(
            _scope_filter(scope, accounts, messages=True),
            pk=row.pk,
            updated_at=row.updated_at,
            conversation_id=conversation.pk,
            workspace_id=row.workspace_id,
            social_account_id=row.social_account_id,
            platform=row.platform,
            platform_message_id=row.platform_message_id,
            legacy_message_id=row.legacy_message_id,
            legacy_reply_id=row.legacy_reply_id,
        ).exists()
        or not InboxConversation.objects.filter(
            _scope_filter(scope, accounts), pk=conversation.pk, revision=conversation.revision
        ).exists()
    ):
        raise CanonicalReadError("stale_revision", "The incoming message changed while reading; reload.")


def read_legacy_message(scope, message):
    from apps.api.schemas import InboxMessageResponse

    from .canonical_reads import CanonicalReadError, _scope_filter, _snapshot, resolve_legacy_conversation

    if not _is_canonical(scope, message):
        return None
    scope = narrow_scope(scope, social_account_ids=[message.social_account_id])
    conversation_id = resolve_legacy_conversation(scope, message.pk)
    accounts, token = _snapshot(scope)
    current = (
        InboxMessage.objects.select_related("social_account")
        .filter(
            pk=message.pk,
            workspace_id=scope.workspace_id,
            social_account_id=message.social_account_id,
            platform_message_id=message.platform_message_id,
            message_type="dm",
        )
        .first()
    )
    conversation = InboxConversation.objects.filter(_scope_filter(scope, accounts), pk=conversation_id).first()
    row = ConversationMessage.objects.filter(
        _scope_filter(scope, accounts, messages=True),
        legacy_message_id=message.pk,
        conversation_id=conversation_id,
        direction="inbound",
    ).first()
    if (
        current is None
        or row is None
        or conversation is None
        or row.platform_message_id != current.platform_message_id
        or row.occurred_at is None
    ):
        raise CanonicalReadError(
            "canonical_unavailable", "This incoming record cannot be represented safely in the legacy contract."
        )
    extra = current.extra if isinstance(current.extra, dict) else {}
    if extra.get("transport_projection") is True and extra.get("canonical_message_id") != str(row.pk):
        raise CanonicalReadError("canonical_unavailable", "This transport projection identity changed.")
    content = visible_content(row)
    view = copy(current)
    view._state = copy(current._state)
    view.body, view.sender_name, view.sender_handle, view.received_at = (
        content["body"],
        row.sender_name,
        row.sender_id,
        row.occurred_at,
    )
    # No original media keys survive: they must not resurrect a restricted URL.
    view.extra = {
        "inbox_attachments": content["attachments"],
        "is_deleted": content["is_deleted"],
        "inbox_content_status": "unavailable" if content["is_expired"] else content["content_status"],
        "conversation_type": conversation.conversation_type,
        "classification_reason": conversation.classification_reason,
    }
    if extra.get("transport_projection") is True:
        view.extra.update(transport_projection=True, canonical_message_id=str(row.pk))
    value = InboxMessageResponse.from_message(view, include_replies=False, include_eligibility=False).model_dump(
        mode="json"
    )
    value.update(
        canonical_content_status=value["content_status"],
        id_namespace="inbox_message",
        legacy_status_deprecated=True,
    )
    if not content["available"] or value["content_status"] in {"expired", "removed"}:
        value["content_preview"] = (
            "Content expired"
            if content["is_expired"] or value["content_status"] == "expired"
            else "Message withdrawn"
            if content["is_deleted"] or value["content_status"] == "removed"
            else "Content unavailable"
        )
    value["replies"] = [_receipt(reply) for reply in current.replies.select_related("author")]
    value["reply_eligibility"] = {
        "allowed": False,
        "code": "canonical_composer_required",
        "reason": "Use the canonical conversation composer with current authorization.",
        "existing_reply_id": None,
        "requires_current_authorization": True,
    }
    value.update(_metadata(scope, conversation))
    _stable(scope, accounts, token, row, conversation)
    return value


def read_canonical_incoming_message(scope, message_id):
    from .canonical_reads import _denied, _iso, _scope_filter, _size, _snapshot, _uuid, project_message

    scope = narrow_scope(scope, target=(ConversationMessage, message_id))
    accounts, token = _snapshot(scope)
    row = ConversationMessage.objects.filter(
        _scope_filter(scope, accounts, messages=True),
        pk=_uuid(message_id),
        direction="inbound",
        conversation__isnull=False,
    ).first()
    if row is None:
        raise _denied()
    conversation = InboxConversation.objects.filter(_scope_filter(scope, accounts), pk=row.conversation_id).first()
    if conversation is None:
        raise _denied()
    query = (
        InboxReply.objects.filter(
            Q(conversation=conversation) | Q(conversation_message__conversation=conversation),
            inbox_message__workspace_id=scope.workspace_id,
            inbox_message__social_account_id=row.social_account_id,
            inbox_message__message_type="dm",
        )
        .select_related("author")
        .distinct()
        .order_by("-created_at", "-pk")
    )
    replies = []
    for reply in query[:10]:
        item = _receipt(reply)
        item["body_truncated"] = len(item["body"]) > 2000
        item["body"], item["send_error"] = item["body"][:2000], item["send_error"][:500]
        if replies and _size(replies + [item]) > 16000:
            break
        replies.append(item)
    value = {
        **project_message(row),
        **_metadata(scope, conversation),
        "id_namespace": "canonical_message",
        "workspace_id": str(row.workspace_id),
        "social_account_id": str(row.social_account_id),
        "platform": row.platform,
        "message_type": "dm",
        "sender_handle": row.sender_id,
        "received_at": _iso(row.occurred_at),
        "created_at": _iso(row.first_seen_at),
        "status": conversation.workflow_state or "unclassified",
        "status_domain": "conversation_workflow",
        "sentiment": "",
        "legacy_status_deprecated": True,
        "replies": replies,
        "replies_scope": "genuine_brightbean_conversation_receipts",
        "replies_truncated": query.count() > len(replies) or any(item["body_truncated"] for item in replies),
        "reply_eligibility": {
            "allowed": False,
            "code": "canonical_composer_required",
            "existing_reply_id": None,
            "reason": "This canonical incoming ID is not a legacy send target.",
            "requires_current_authorization": True,
        },
    }
    _stable(scope, accounts, token, row, conversation)
    return value


def legacy_body_search_query(search):
    """Legacy-only body matching never consults a shadowed DM's raw body.

    Canonical body search belongs to list_conversations. Caller applies its
    usual current actor scope and may OR safe sender/account metadata matches.
    """
    from django.db.models import Exists, OuterRef

    from .canonical_content import legacy_content_restriction_query

    shadows = ConversationMessage.objects.filter(
        Q(legacy_message_id=OuterRef("pk"))
        | (
            Q(social_account_id=OuterRef("social_account_id"), platform_message_id=OuterRef("platform_message_id"))
            & ~Q(platform_message_id="")
        )
    )
    protected = (
        InboxMessage.objects.filter(message_type="dm")
        .annotate(canonical_shadow=Exists(shadows))
        .filter(Q(canonical_shadow=True) | legacy_content_restriction_query())
        .values("pk")
    )
    return Q(body__icontains=search) & ~Q(pk__in=protected)
