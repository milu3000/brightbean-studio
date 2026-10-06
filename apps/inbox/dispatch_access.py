"""Current-grant adapters for the explicitly enrolled V2 dispatch service.

No bearer, callback or new grant is stored. Principal names come only from the
authenticated API actor, never from caller JSON. Existing send permissions do
not enroll an account or conversation in this opt-in workflow.
"""

from django.core.exceptions import PermissionDenied

from apps.api.auth import _resolve_oauth_actor
from apps.api_keys.models import ApiKey
from apps.members.models import WorkspaceMembership

from .conversation_policy import read_allowed
from .dm_send_gate import key_send_authorization
from .models import SendOperation
from .reply_coordination import ReplyActorScope, ReplyCoordinationError


def dispatch_actor(api_key, request):
    """Return a pinned principal and a closure that checks live read/send grants."""
    workspace_id, user_id = api_key.workspace_id, api_key.issued_by_id
    is_oauth = getattr(api_key, "is_oauth", False)
    principal = f"oauth:{user_id}" if is_oauth else f"key:{api_key.pk}"
    accounts = frozenset(api_key.social_accounts.all().filter(workspace_id=workspace_id).values_list("pk", flat=True))
    send_authorization = key_send_authorization(api_key, request)

    def authorize(account):
        send_authorization(account)
        member = (
            WorkspaceMembership.objects.select_related("custom_role", "workspace")
            .filter(
                user_id=user_id,
                user__is_active=True,
                workspace_id=workspace_id,
                workspace__is_archived=False,
            )
            .first()
        )
        if member is None or not member.effective_permissions.get("use_inbox", False):
            raise ReplyCoordinationError("not_found_or_denied")
        if is_oauth:
            header = request.headers.get("Authorization", "") if request is not None else ""
            current = _resolve_oauth_actor(header[7:]) if header.startswith("Bearer ") else None
            allowed = (
                current is not None
                and current.workspace_id == workspace_id
                and current.issued_by_id == user_id
                and current.effective_permissions.get("use_inbox", False)
            )
        else:
            key = ApiKey.objects.filter(pk=api_key.pk, workspace_id=workspace_id, issued_by_id=user_id).first()
            allowed = key is not None and key.is_active and "use_inbox" in (key.permissions or [])
        if not allowed:
            raise ReplyCoordinationError("not_found_or_denied")

    authorize.actor_scope = principal
    return ReplyActorScope(principal, workspace_id, accounts, True), authorize


def visible_operation(scope, operation_id, authorization):
    """No operation bodies, lease claims or foreign principal history leakage."""
    operation = (
        SendOperation.objects.select_related("social_account")
        .filter(
            pk=operation_id,
            workspace_id=scope.workspace_id,
            social_account_id__in=scope.allowed_account_ids,
            actor_scope=scope.actor_id,
        )
        .first()
    )
    if operation is None:
        raise ReplyCoordinationError("not_found_or_denied")
    authorization(operation.social_account)
    if (
        not read_allowed(operation.social_account)
        or operation.social_account.workspace_id != scope.workspace_id
        or operation.platform != operation.social_account.platform
    ):
        raise ReplyCoordinationError("not_found_or_denied")
    return operation


def operation_result(operation, *, include_claim=False):
    """Claims are returned only to the caller who just obtained that claim."""
    result = {
        "operation_id": str(operation.pk),
        "conversation_id": str(operation.conversation_id),
        "social_account_id": str(operation.social_account_id),
        "target_message_id": str(operation.target_id) if operation.target_id else None,
        "status": operation.status,
        "expected_revision": operation.expected_revision,
        "expected_generation": operation.expected_generation,
        "owner_epoch": operation.owner_epoch,
        "reply_id": str(operation.reply_id) if operation.reply_id else None,
        "attempt_id": str(operation.attempt_id) if operation.attempt_id else None,
        "outcome_code": operation.outcome_code,
        "freshness_complete": False,
        "external_atomicity": False,
        "note": "Fenced observed-state dispatch only; native or external platform activity may arrive later.",
    }
    if include_claim:
        result.update(
            claim_token=str(operation.claim_token) if operation.claim_token else None,
            fencing_token=operation.fencing_token,
            lease_expires_at=operation.lease_expires_at.isoformat() if operation.lease_expires_at else None,
        )
    return result


def require_request_permissions(membership):
    if membership is None or not all(
        membership.effective_permissions.get(permission, False) for permission in ("use_inbox", "reply_from_inbox")
    ):
        raise PermissionDenied("Current inbox read and reply permissions are required.")
