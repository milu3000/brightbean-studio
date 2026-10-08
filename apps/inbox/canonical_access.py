"""Fresh actor/account grants and immutable saved-history scope, without I/O."""

import hashlib
import json
from copy import copy
from dataclasses import dataclass
from uuid import UUID

from django.conf import settings
from django.db.models import F, Q
from django.utils.crypto import salted_hmac

from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount

from .conversation_policy import read_allowed


class CanonicalReadError(ValueError):
    def __init__(self, code, message, *, data=None):
        self.code, self.data = code, data
        super().__init__(message)


def enabled():
    return getattr(settings, "INBOX_CANONICAL_READ_ENABLED", False) is True


def denied():
    return CanonicalReadError("not_found_or_denied", "This saved conversation is unavailable in the current scope.")


def identifier(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise denied() from exc


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def member(user_id, workspace_id):
    value = (
        WorkspaceMembership.objects.select_related("workspace", "custom_role")
        .filter(user_id=user_id, user__is_active=True, workspace_id=workspace_id, workspace__is_archived=False)
        .first()
    )
    if (
        value is None
        or value.effective_permissions.get("use_inbox") is not True
        or (value.custom_role_id and value.custom_role.organization_id != value.workspace.organization_id)
    ):
        raise denied()
    return value


@dataclass(frozen=True)
class CanonicalReadScope:
    workspace_id: UUID
    principal: str
    refresh: object
    user_id: UUID


def session_read_scope(user, workspace_id):
    workspace_id, user_id = identifier(workspace_id), getattr(user, "pk", None)

    def refresh():
        current = member(user_id, workspace_id)
        return SocialAccount.objects.filter(workspace_id=workspace_id), [
            str(current.pk),
            current.workspace_role,
            str(current.custom_role_id),
            current.effective_permissions,
        ]

    return CanonicalReadScope(workspace_id, f"session:{user_id}", refresh, user_id)


def key_read_scope(api_key, request=None):
    workspace_id, user_id, key_id = api_key.workspace_id, api_key.issued_by_id, getattr(api_key, "pk", None)
    oauth = bool(getattr(api_key, "is_oauth", False))
    header = request.headers.get("Authorization", "") if request is not None else ""
    credential = None if oauth else (api_key.token_hash, api_key.lookup_prefix)
    credential_digest = salted_hmac(
        "inbox.canonical-read.principal.v1", header if oauth else repr(credential)
    ).hexdigest()

    def refresh():
        from apps.api.auth import _resolve_oauth_actor
        from apps.api_keys.models import ApiKey

        current_member = member(user_id, workspace_id)
        if oauth:
            if request is None or request.headers.get("Authorization", "") != header:
                raise denied()
            current = _resolve_oauth_actor(header[7:]) if header.startswith("Bearer ") else None
            valid = bool(
                current is not None
                and current.workspace_id == workspace_id
                and current.issued_by_id == user_id
                and current.effective_permissions.get("use_inbox") is True
            )
            grants = current.effective_permissions if valid else {}
        else:
            current = ApiKey.objects.filter(pk=key_id, workspace_id=workspace_id, issued_by_id=user_id).first()
            valid = bool(
                current is not None
                and current.is_active
                and (current.token_hash, current.lookup_prefix) == credential
                and isinstance(current.permissions, list)
                and "use_inbox" in current.permissions
            )
            grants = sorted(current.permissions) if valid else []
        if not valid:
            raise denied()
        return current.social_accounts.all().filter(workspace_id=workspace_id), [
            credential_digest,
            grants,
            str(current_member.pk),
            current_member.workspace_role,
            str(current_member.custom_role_id),
            current_member.effective_permissions,
        ]

    return CanonicalReadScope(
        identifier(workspace_id), f"{'oauth' if oauth else 'key'}:{key_id}:{user_id}", refresh, user_id
    )


def narrow_scope(scope, *, social_account_ids=None, platforms=None, target=None):
    """Restrict an existing actor scope without replacing its fresh grants.

    A target is a persisted inbox row. Its account is only a selector: the
    original scope and every subsequent snapshot still enforce authorization.
    """
    if not isinstance(scope, CanonicalReadScope):
        raise denied()
    selected = {identifier(value) for value in social_account_ids} if social_account_ids is not None else None
    if target is not None:
        model, pk = target
        account_id = (
            model.objects.filter(
                pk=identifier(pk), workspace_id=scope.workspace_id, social_account__workspace_id=scope.workspace_id
            )
            .values_list("social_account_id", flat=True)
            .first()
        )
        if account_id is None or (selected is not None and account_id not in selected):
            raise denied()
        selected = {account_id}
    if selected is None and not platforms:
        return scope

    def refresh():
        accounts, grants = scope.refresh()
        accounts = accounts.filter(workspace_id=scope.workspace_id)
        if selected is not None:
            accounts = accounts.filter(pk__in=selected)
            if set(accounts.values_list("pk", flat=True)) != selected:
                raise denied()
        if platforms:
            accounts = accounts.filter(platform__in=platforms)
        return accounts, grants

    return CanonicalReadScope(scope.workspace_id, scope.principal, refresh, scope.user_id)


def archive_identity(account):
    from .models import InboxArchiveIdentity

    if account.connection_status != "disconnected":
        return None
    return (
        InboxArchiveIdentity.objects.filter(
            social_account_id=account.pk,
            workspace_id=account.workspace_id,
            platform=account.platform,
            account_platform_id=account.account_platform_id,
            webhook_target_id=account.webhook_target_id,
        )
        .filter(
            Q(archived_connection__isnull=True, connection_generation__isnull=True)
            | Q(
                archived_connection__social_account_id=account.pk,
                archived_connection__workspace_id=account.workspace_id,
                archived_connection__platform=account.platform,
                connection_generation__isnull=False,
            )
        )
        .select_related("archived_connection")
        .order_by("-archived_at", "-pk")
        .first()
    )


def read_connection(account):
    """Saved reads require immutable identity, never a current provider token."""
    from .sync_identity import canonical_read_connection

    current = (
        SocialAccount.objects.filter(pk=account.pk, workspace_id=account.workspace_id)
        .only("pk", "workspace_id", "platform", "account_platform_id", "webhook_target_id", "connection_status")
        .first()
    )
    if current is None or current.platform != account.platform:
        raise denied()
    archived = archive_identity(current)
    if archived is not None:
        connection = copy(archived.archived_connection) if archived.archived_connection_id else None
        if connection:
            connection.generation = archived.connection_generation
        return connection, archived
    if current.connection_status == "disconnected":
        raise CanonicalReadError(
            "canonical_provenance_unverified", "The disconnected history requires an archive identity."
        )
    try:
        return canonical_read_connection(current), None
    except Exception as exc:
        # No provider diagnostics or credentials cross the public read boundary.
        if not hasattr(exc, "code"):
            raise
        raise CanonicalReadError(
            "canonical_provenance_unverified", "The saved inbox identity requires review."
        ) from exc


def snapshot(scope, *, require_enabled=True):
    if not isinstance(scope, CanonicalReadScope) or (require_enabled and not enabled()):
        raise denied()
    queryset, grants = scope.refresh()
    rows = list(
        queryset.only(
            "pk",
            "workspace_id",
            "platform",
            "account_platform_id",
            "webhook_target_id",
            "account_name",
            "account_handle",
            "connection_status",
            "connected_at",
        ).order_by("pk")
    )
    accounts = {}
    for account in rows:
        if account.workspace_id != scope.workspace_id:
            continue
        if not read_allowed(account) and archive_identity(account) is None:
            from .sync_identity import canonical_owns_account

            if canonical_owns_account(account):
                raise CanonicalReadError(
                    "canonical_unavailable", "The saved canonical account requires its existing read enrollment."
                )
            continue
        account._canonical_connection, account._canonical_archive = read_connection(account)
        account._read_user_id = scope.user_id
        accounts[account.pk] = account
    identities = [
        [
            item.pk,
            item.workspace_id,
            item.platform,
            item.account_platform_id,
            item.webhook_target_id,
            item.connection_status,
            item.connected_at,
            item._canonical_connection.pk if item._canonical_connection else None,
            item._canonical_connection.generation if item._canonical_connection else None,
            item._canonical_archive.pk if item._canonical_archive else None,
        ]
        for item in accounts.values()
    ]
    return accounts, digest([scope.principal, scope.workspace_id, grants, [row.pk for row in rows], identities])


def recheck(scope, expected, *, require_enabled=True):
    if snapshot(scope, require_enabled=require_enabled)[1] != expected:
        raise CanonicalReadError("stale_scope", "The current inbox grants or account scope changed; reload.")


def legacy_links():
    from .canonical_content import native_receipt_relocation_query

    incoming = Q(legacy_message__isnull=True) | Q(
        legacy_message__workspace_id=F("workspace_id"),
        legacy_message__social_account_id=F("social_account_id"),
        legacy_message__social_account__workspace_id=F("workspace_id"),
        legacy_message__social_account__platform=F("platform"),
        legacy_message__message_type="dm",
    )
    outgoing = Q(legacy_reply__isnull=True) | (
        Q(
            legacy_reply__inbox_message__workspace_id=F("workspace_id"),
            legacy_reply__inbox_message__social_account_id=F("social_account_id"),
            legacy_reply__inbox_message__social_account__workspace_id=F("workspace_id"),
            legacy_reply__inbox_message__social_account__platform=F("platform"),
            legacy_reply__inbox_message__message_type="dm",
        )
        & (
            Q(legacy_reply__conversation__isnull=True)
            | Q(legacy_reply__conversation_id=F("conversation_id"))
            | native_receipt_relocation_query()
        )
    )
    return incoming & outgoing


def scope_filter(scope, accounts, *, messages=False, unassigned=False):
    from .canonical_content import archived_message_provenance
    from .models import ConversationMessage

    identities = Q(pk__in=[])
    for account in accounts.values():
        candidate = Q(social_account_id=account.pk, platform=account.platform)
        connection, archive = account._canonical_connection, account._canonical_archive
        if unassigned:
            if not messages or connection is None:
                continue
            candidate &= Q(conversation__isnull=True, observation_state__connection_generation=connection.generation)
        elif connection is not None:
            prefix = "conversation__sync_identity" if messages else "sync_identity"
            candidate &= Q(
                **{f"{prefix}__connection_id": connection.pk, f"{prefix}__connection_generation": connection.generation}
            )
            if messages:
                candidate &= Q(observation_state__connection_generation=connection.generation)
        elif archive is not None:
            proof = archived_message_provenance(account, archive)
            candidate &= (
                proof if messages else Q(pk__in=ConversationMessage.objects.filter(proof).values("conversation_id"))
            )
        identities |= candidate
    result = identities & Q(workspace_id=scope.workspace_id, social_account__workspace_id=scope.workspace_id)
    return result & legacy_links() if messages else result
