"""Default message-content policy for UI, API, AI and legacy projections.

This module never reads retained/recovery content. Actor authorization remains
the caller's responsibility; stored source identity is independently checked.
"""

from django.db.models import Q

from providers.meta_inbox_content import message_content_status, normalize_attachments

WITHDRAWN_STATUSES = frozenset({"removed", "withdrawn", "deleted", "unsent"})
PUBLIC_STATUSES = frozenset(
    {"partial", "unsupported", "fields_unavailable", "link_provided", "unavailable", "text", "no_metadata"}
)


def _current(row):
    from .models import ConversationMessage

    fields = (
        "workspace_id",
        "social_account_id",
        "platform",
        "conversation_id",
        "platform_message_id",
        "legacy_message_id",
        "legacy_reply_id",
    )
    # A deferred identity could be loaded only after a source rebind. Require
    # the caller's original complete identity rather than adopting that scope.
    if set(fields) & row.get_deferred_fields():
        return None
    return (
        ConversationMessage.objects.filter(pk=row.pk, **{key: getattr(row, key) for key in fields})
        .select_related("observation_state")
        .defer(
            "observation_state__retained_body",
            "observation_state__retained_attachments",
            "observation_state__retained_legacy_body",
            "observation_state__retained_legacy_attachments",
        )
        .first()
    )


def archived_message_provenance(account, archive):
    """Predurable history needs own-endpoint evidence captured before archive."""
    own = {account.account_platform_id, account.webhook_target_id} - {""}
    if not own:
        return Q(pk__in=[])
    return Q(
        workspace_id=account.workspace_id,
        social_account_id=account.pk,
        platform=account.platform,
        conversation__workspace_id=account.workspace_id,
        conversation__social_account_id=account.pk,
        conversation__platform=account.platform,
        observation_state__isnull=True,
        first_seen_at__lte=archive.archived_at,
    ) & (
        (Q(direction="inbound", recipient_id__in=own) & ~Q(sender_id__in=own) & ~Q(sender_id=""))
        | Q(direction="outbound", sender_id__in=own)
    )


def _provenance(row, state):
    from apps.social_accounts.models import SocialAccount

    from .canonical_access import read_connection
    from .models import ConversationMessage, ConversationSyncIdentity, InboxConversation, InboxMessage, InboxReply

    account = (
        SocialAccount.objects.filter(pk=row.social_account_id, workspace_id=row.workspace_id, platform=row.platform)
        .only("id", "workspace_id", "platform", "account_platform_id", "webhook_target_id", "connection_status")
        .first()
    )
    if account is None:
        return False
    if (
        row.conversation_id
        and not InboxConversation.objects.filter(
            pk=row.conversation_id,
            workspace_id=row.workspace_id,
            social_account_id=row.social_account_id,
            platform=row.platform,
        ).exists()
    ):
        return False
    if (
        row.legacy_message_id
        and not InboxMessage.objects.filter(
            pk=row.legacy_message_id,
            workspace_id=row.workspace_id,
            social_account_id=row.social_account_id,
            message_type="dm",
        ).exists()
    ):
        return False
    if (
        row.legacy_reply_id
        and not InboxReply.objects.filter(
            pk=row.legacy_reply_id,
            inbox_message__workspace_id=row.workspace_id,
            inbox_message__social_account_id=row.social_account_id,
            inbox_message__social_account__workspace_id=row.workspace_id,
            inbox_message__social_account__platform=row.platform,
            inbox_message__message_type="dm",
        ).exists()
    ):
        return False
    if row.legacy_reply_id:
        intent = InboxReply.objects.filter(pk=row.legacy_reply_id).values_list("conversation_id", flat=True).first()
        if intent is not None and intent != row.conversation_id and not native_receipt_relocation_allowed(row):
            return False
    try:
        connection, archive = read_connection(account)
    except ValueError:
        return False
    if connection is None:
        return (
            archive is None
            or ConversationMessage.objects.filter(archived_message_provenance(account, archive), pk=row.pk).exists()
        )
    return bool(
        state is not None
        and state.connection_generation == connection.generation
        and (
            row.conversation_id is None
            or ConversationSyncIdentity.objects.filter(
                conversation_id=row.conversation_id,
                connection_id=connection.pk,
                connection_generation=connection.generation,
            ).exists()
        )
    )


def _snapshot(row, *, provenance_checked):
    row = row if provenance_checked else _current(row)
    if row is None:
        return None, {
            "available": False,
            "is_deleted": False,
            "is_expired": False,
            "content_status": "unavailable",
            "reason": "not_found",
        }
    # The fast-path caller may not have prefetched the sidecar. Fetch only
    # policy metadata; a default read must not load the restricted archive.
    if "observation_state" in row._state.fields_cache:
        state = row._state.fields_cache["observation_state"]
    else:
        from .models import ConversationObservationState

        state = (
            ConversationObservationState.objects.filter(message_id=row.pk)
            .only("message_id", "connection_generation", "withdrawn_at", "expired_at", "expires_at")
            .first()
        )

    legacy_restriction = legacy_message_restriction(row)
    withdrawn = bool(
        row.is_deleted
        or row.content_status in WITHDRAWN_STATUSES
        or (state and state.withdrawn_at)
        or legacy_restriction == "withdrawn"
    )
    # expires_at is a planned deadline, not an activated expiry action. Only
    # an explicit applied restriction changes default visibility.
    expired = bool(row.content_status == "expired" or (state and state.expired_at) or legacy_restriction == "expired")
    permitted = provenance_checked or _provenance(row, state)
    status = (
        row.content_status
        if row.content_status in PUBLIC_STATUSES
        else message_content_status({"inbox_attachments": row.attachments}, row.body or "")
    )
    return row, {
        "available": bool(permitted and not withdrawn and not expired),
        "is_deleted": withdrawn,
        "is_expired": expired,
        "content_status": "expired" if expired else "removed" if withdrawn else status if permitted else "unavailable",
        "reason": "expired" if expired else "withdrawn" if withdrawn else "" if permitted else "provenance_unverified",
    }


def content_visibility(row, *, provenance_checked=False):
    """Only an already-scoped internal reader may use the trusted fast path."""
    return _snapshot(row, provenance_checked=provenance_checked)[1]


def visible_content(row, *, provenance_checked=False):
    current, visibility = _snapshot(row, provenance_checked=provenance_checked)
    return {
        **visibility,
        "body": (current.body or "") if visibility["available"] else "",
        "attachments": normalize_attachments({"inbox_attachments": current.attachments})
        if visibility["available"]
        else [],
    }


def native_receipt_relocation_query():
    """Exact native placement may differ from immutable send intent.

    This predicate is evaluated against ConversationMessage. It permits no
    peer-only inference: both threads and the exact accepted provider ID must
    share current durable account/generation and original recipient proof.
    """
    from django.db.models import Exists, F, OuterRef

    from .models import DMSendAttempt, InboxArchiveIdentity

    unresolved = DMSendAttempt.objects.filter(reply_id=OuterRef("legacy_reply_id")).exclude(
        outcome="sent",
        completed_at__isnull=False,
        control__social_account_id=OuterRef("social_account_id"),
        control__workspace_id=OuterRef("workspace_id"),
        control__platform=OuterRef("platform"),
        control__account_platform_id=OuterRef("sender_id"),
    )
    # Disconnect rotates the live lease generation. Saved history retains the
    # exact pre-disconnect generation through its immutable archive identity.
    archived = InboxArchiveIdentity.objects.filter(
        social_account_id=OuterRef("social_account_id"),
        workspace_id=OuterRef("workspace_id"),
        platform=OuterRef("platform"),
        account_platform_id=OuterRef("sender_id"),
        webhook_target_id=OuterRef("social_account__webhook_target_id"),
        archived_connection_id=OuterRef("conversation__sync_identity__connection_id"),
        connection_generation=OuterRef("observation_state__connection_generation"),
        social_account__connection_status="disconnected",
    )
    return (
        Q(
            direction="outbound",
            delivery_status="observed",
            conversation_attribution="platform",
            conversation_type="direct",
            conversation__conversation_type="direct",
            legacy_reply__status="sent",
            legacy_reply__sent_at__isnull=False,
            legacy_reply__send_generation__gte=1,
            legacy_reply__conversation__isnull=False,
            legacy_reply__connection_generation__isnull=False,
            platform_message_id=F("legacy_reply__platform_reply_id"),
            sender_id=F("social_account__account_platform_id"),
            recipient_id=F("legacy_reply__recipient_id"),
            legacy_reply__account_platform_id=F("social_account__account_platform_id"),
            legacy_reply__inbox_message__workspace_id=F("workspace_id"),
            legacy_reply__inbox_message__social_account_id=F("social_account_id"),
            legacy_reply__inbox_message__message_type="dm",
            legacy_reply__conversation__workspace_id=F("workspace_id"),
            legacy_reply__conversation__social_account_id=F("social_account_id"),
            legacy_reply__conversation__platform=F("platform"),
            legacy_reply__conversation__conversation_type="direct",
            legacy_reply__conversation__platform_conversation_id=F("legacy_reply__platform_conversation_id"),
            legacy_reply__conversation__peer_id=F("recipient_id"),
            conversation__workspace_id=F("workspace_id"),
            conversation__social_account_id=F("social_account_id"),
            conversation__platform=F("platform"),
            conversation__peer_id=F("recipient_id"),
            social_account__workspace_id=F("workspace_id"),
            social_account__platform=F("platform"),
            observation_state__connection_generation=F("legacy_reply__connection_generation"),
            conversation__sync_identity__connection_generation=F("observation_state__connection_generation"),
            conversation__sync_identity__connection__social_account_id=F("social_account_id"),
            conversation__sync_identity__connection__workspace_id=F("workspace_id"),
            conversation__sync_identity__connection__platform=F("platform"),
            conversation__sync_identity__connection__account_platform_id=F("social_account__account_platform_id"),
            conversation__sync_identity__connection__webhook_target_id=F("social_account__webhook_target_id"),
            legacy_reply__conversation__sync_identity__connection_id=F("conversation__sync_identity__connection_id"),
            legacy_reply__conversation__sync_identity__connection_generation=F(
                "observation_state__connection_generation"
            ),
        )
        & ~Q(platform_message_id="")
        & ~Q(sender_id="")
        & ~Q(recipient_id="")
        & ~Q(conversation__platform_conversation_id="")
        & Q(conversation__platform_conversation_id__isnull=False)
        & (
            Q(conversation__sync_identity__connection__generation=F("observation_state__connection_generation"))
            | Exists(archived)
        )
        & ~Exists(unresolved)
    )


def native_receipt_relocation_allowed(row, reply=None):
    from .models import ConversationMessage

    current = _current(row)
    if current is None or (reply is not None and current.legacy_reply_id != reply.pk):
        return False
    return ConversationMessage.objects.filter(native_receipt_relocation_query(), pk=current.pk).exists()


def legacy_content_restriction(extra):
    """Applied legacy markers only; malformed truthy values are not evidence."""
    from providers.meta_inbox_content import is_deleted_content

    data = extra if isinstance(extra, dict) else {}
    if data.get("content_status") == "expired" or data.get("inbox_content_status") == "expired":
        return "expired"
    return "withdrawn" if is_deleted_content(data) else ""


def legacy_content_restriction_query():
    """Typed JSON comparisons: SQLite's usual key lookup conflates true with "true"."""
    from django.db.models import BooleanField, F, Func
    from django.db.utils import NotSupportedError

    class AppliedRestriction(Func):
        output_field = BooleanField()
        arity = 1

        def as_sqlite(self, compiler, connection):
            sql, params = compiler.compile(self.source_expressions[0])
            return (
                f"COALESCE((JSON_TYPE({sql}, '$.is_deleted') = 'true' OR "
                f"JSON_TYPE({sql}, '$.message.is_deleted') = 'true' OR "
                f"JSON_EXTRACT({sql}, '$.content_status') = 'expired' OR "
                f"JSON_EXTRACT({sql}, '$.inbox_content_status') = 'expired'), 0)",
                params * 4,
            )

        def as_postgresql(self, compiler, connection):
            sql, params = compiler.compile(self.source_expressions[0])
            return (
                f"COALESCE((({sql} -> 'is_deleted') = 'true'::jsonb OR "
                f"({sql} #> '{{message,is_deleted}}') = 'true'::jsonb OR "
                f"({sql} ->> 'content_status') = 'expired' OR "
                f"({sql} ->> 'inbox_content_status') = 'expired'), FALSE)",
                params * 4,
            )

        def as_sql(self, compiler, connection):
            raise NotSupportedError("Legacy applied content restrictions require PostgreSQL or SQLite.")

    return Q(AppliedRestriction(F("extra")))


def _legacy_source_query(row):
    from .models import InboxMessage

    identity = Q(pk=row.legacy_message_id) if row.legacy_message_id else Q(pk__in=[])
    if row.platform_message_id:
        identity |= Q(platform_message_id=row.platform_message_id)
    return InboxMessage.objects.filter(
        identity,
        message_type="dm",
        workspace_id=row.workspace_id,
        social_account_id=row.social_account_id,
        social_account__workspace_id=row.workspace_id,
        social_account__platform=row.platform,
        platform_message_id=row.platform_message_id,
    )


def legacy_message_restriction(row):
    """Applied incoming tombstones survive import/link timing differences."""
    if row.direction == "outbound":
        return ""
    reasons = [
        legacy_content_restriction(extra) for extra in _legacy_source_query(row).values_list("extra", flat=True)[:2]
    ]
    return "expired" if "expired" in reasons else "withdrawn" if "withdrawn" in reasons else ""


def canonical_legacy_restriction_query():
    from django.db.models import Exists, OuterRef

    from .models import InboxMessage

    originals = InboxMessage.objects.filter(
        Q(pk=OuterRef("legacy_message_id"))
        | (Q(platform_message_id=OuterRef("platform_message_id")) & ~Q(platform_message_id="")),
        message_type="dm",
        workspace_id=OuterRef("workspace_id"),
        social_account_id=OuterRef("social_account_id"),
        social_account__workspace_id=OuterRef("workspace_id"),
        social_account__platform=OuterRef("platform"),
        platform_message_id=OuterRef("platform_message_id"),
    ).filter(legacy_content_restriction_query())
    return ~Q(direction="outbound") & Q(Exists(originals))
