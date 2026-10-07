"""Fresh existing inbox permission for drafts; send permission stays separate."""

from apps.members.models import WorkspaceMembership

from .dm_send_gate import DMSendGateError


def session_read_authorization(user):
    user_id = getattr(user, "pk", None)

    def authorize(account):
        member = (
            WorkspaceMembership.objects.select_related("custom_role", "workspace")
            .filter(
                user_id=user_id,
                user__is_active=True,
                workspace_id=account.workspace_id,
                workspace__is_archived=False,
            )
            .first()
        )
        if (
            member is None
            or not member.effective_permissions.get("use_inbox", False)
            or (member.custom_role_id and member.custom_role.organization_id != member.workspace.organization_id)
        ):
            raise DMSendGateError("authorization_revoked", "Current permission to use this inbox is unavailable.")

    authorize.actor_scope = f"user:{user_id}"

    def canonical_scope(account):
        from .canonical_access import session_read_scope

        return session_read_scope(user, account.workspace_id)

    authorize.canonical_scope = canonical_scope
    return authorize


def key_read_authorization(api_key, request=None):
    workspace_id, actor_id = api_key.workspace_id, api_key.issued_by_id
    header = request.headers.get("Authorization", "") if request is not None else ""
    credential = None if getattr(api_key, "is_oauth", False) else (api_key.token_hash, api_key.lookup_prefix)

    def authorize(account):
        from apps.api.auth import _resolve_oauth_actor
        from apps.api_keys.models import ApiKey

        if account.workspace_id != workspace_id:
            raise DMSendGateError("authorization_revoked", "Current permission to use this inbox is unavailable.")
        if getattr(api_key, "is_oauth", False):
            if request is None or request.headers.get("Authorization", "") != header:
                raise DMSendGateError("authorization_revoked", "Current permission to use this inbox is unavailable.")
            current = _resolve_oauth_actor(header[7:]) if header.startswith("Bearer ") else None
            permitted = bool(
                current
                and current.workspace_id == workspace_id
                and current.issued_by_id == actor_id
                and current.effective_permissions.get("use_inbox", False)
                and current.social_accounts.all().filter(pk=account.pk, workspace_id=workspace_id).exists()
            )
        else:
            current = ApiKey.objects.filter(pk=api_key.pk, workspace_id=workspace_id, issued_by_id=actor_id).first()
            permitted = bool(
                current
                and current.is_active
                and (current.token_hash, current.lookup_prefix) == credential
                and "use_inbox" in (current.permissions or [])
                and current.social_accounts.all().filter(pk=account.pk, workspace_id=workspace_id).exists()
            )
        if not permitted:
            raise DMSendGateError("authorization_revoked", "Current permission to use this inbox is unavailable.")
        session_read_authorization(api_key.issued_by)(account)

    authorize.actor_scope = f"oauth:{actor_id}" if getattr(api_key, "is_oauth", False) else f"key:{api_key.pk}"

    def canonical_scope(account):
        from .canonical_access import key_read_scope

        return key_read_scope(api_key, request)

    authorize.canonical_scope = canonical_scope
    return authorize
