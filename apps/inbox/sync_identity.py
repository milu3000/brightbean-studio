"""Current native identity proof; saved history never depends on a live token."""

from django.db.models import Q

from providers.meta_inbox_content import classify_conversation_identity

from .models import ConversationSyncIdentity, InboxConversation, InboxSyncConnection


class SyncError(ValueError):
    """Only bounded internal reason codes are returned, never provider payloads."""

    def __init__(self, code, *, retry_after=None):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


def identity_matches(account, connection):
    return account is not None and (
        account.pk,
        account.workspace_id,
        account.platform,
        account.account_platform_id,
        account.webhook_target_id,
    ) == (
        connection.social_account_id,
        connection.workspace_id,
        connection.platform,
        connection.account_platform_id,
        connection.webhook_target_id,
    )


def canonical_read_connection(account):
    """Scope proof only; callers must still check fresh actor/account permissions.

    Credential rotation alone cannot hide saved history. Native identity or
    workspace reassignment fails closed. Archive-aware reads are a separate
    reviewed extension, never a fallback to a new native owner's history.
    """
    from apps.social_accounts.models import SocialAccount

    connection = InboxSyncConnection.objects.filter(social_account_id=account.pk).first()
    if connection is None:
        return None
    current = (
        SocialAccount.objects.only(
            "id", "workspace_id", "platform", "account_platform_id", "webhook_target_id", "connection_status"
        )
        .filter(pk=account.pk)
        .first()
    )
    if not identity_matches(current, connection) or current.workspace_id != account.workspace_id:
        raise SyncError("canonical_provenance_unverified")
    return connection


def canonical_owns_account(account):
    """An old generation or paused bootstrap can never reopen a legacy writer."""
    connection = InboxSyncConnection.objects.filter(social_account_id=account.pk).first()
    if connection is None:
        return False
    return bool(
        connection.enabled
        or connection.ownership_claimed_at
        or connection.bootstrap_baseline_at
        or connection.last_served_at
        or connection.checkpoints.exists()
        or ConversationSyncIdentity.objects.filter(connection=connection).exists()
    )


def classify_participants(account, participants):
    own = {account.account_platform_id, account.webhook_target_id} - {""}
    return classify_conversation_identity(
        {"participant_ids": list(participants)},
        own_ids=own,
        # This anchor classifies a provider's complete native participant set;
        # it does not infer any message direction or construct a pair.
        sender_id=next((value for value in participants if value in own), ""),
    )


def assert_conversation_provenance(account, connection, provider_id, participants):
    _kind, _reason, peer = classify_participants(account, participants)
    candidates = InboxConversation.objects.filter(
        social_account_id=account.pk,
        workspace_id=account.workspace_id,
        platform=account.platform,
    ).filter(Q(platform_conversation_id=provider_id) | Q(platform_conversation_id__isnull=True, peer_id=peer))
    for candidate in candidates:
        if not ConversationSyncIdentity.objects.filter(
            conversation=candidate,
            connection=connection,
            connection_generation=connection.generation,
        ).exists():
            raise SyncError("canonical_provenance_unverified")


def bind_new_conversation(conversation, connection):
    identity, _ = ConversationSyncIdentity.objects.get_or_create(
        conversation=conversation,
        defaults={"connection": connection, "connection_generation": connection.generation},
    )
    if identity.connection_id != connection.pk or identity.connection_generation != connection.generation:
        raise SyncError("canonical_provenance_unverified")
    return identity
