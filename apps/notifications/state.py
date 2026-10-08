"""Recipient/workspace authorization and exact revision-bound read transitions."""

from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.members.models import WorkspaceMembership

from .models import EventType, Notification

SALT = "notifications.read-snapshot.v1"


def visible_notifications(user, workspace=None, *, lock_memberships=False):
    memberships = WorkspaceMembership.objects.filter(user=user, workspace__is_archived=False).select_related(
        "custom_role", "workspace"
    )
    if workspace is not None:
        memberships = memberships.filter(workspace_id=workspace.pk)
    if lock_memberships:
        memberships = memberships.select_for_update(of=("self",))
    memberships = [
        member
        for member in memberships
        if not member.custom_role_id or member.custom_role.organization_id == member.workspace.organization_id
    ]

    def scoped(ids):
        return Q(workspace_id__in=ids) | Q(workspace__isnull=True, data__workspace_id__in=ids)

    all_ids = [str(member.workspace_id) for member in memberships]
    inbox_ids = [
        str(member.workspace_id) for member in memberships if member.effective_permissions.get("use_inbox") is True
    ]
    personal = Q(workspace__isnull=True) & ~Q(data__has_key="workspace_id")
    return (
        Notification.objects.filter(user=user, superseded_by__isnull=True)
        .filter(scoped(all_ids) | personal)
        .filter(~Q(event_type=EventType.NEW_INBOX_MESSAGE) | scoped(inbox_ids))
    )


def scope_key(request):
    workspace = getattr(request, "workspace", None)
    return str(workspace.pk) if workspace else ""


def snapshot_for_rows(request, rows):
    return signing.dumps(
        {
            "user": str(request.user.pk),
            "workspace": scope_key(request),
            "rows": [(str(pk), revision) for pk, revision in rows],
        },
        salt=SALT,
        compress=True,
    )


def snapshot(request, queryset):
    return snapshot_for_rows(request, queryset.values_list("pk", "revision"))


def snapshot_rows(request, token, queryset):
    if not token:
        return list(queryset.values_list("pk", "revision"))
    value = signing.loads(token, salt=SALT, max_age=86400)
    if value.get("user") != str(request.user.pk) or value.get("workspace") != scope_key(request):
        raise signing.BadSignature("The notification view changed.")
    rows = value.get("rows")
    if not isinstance(rows, list) or any(
        not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], int) or isinstance(row[1], bool)
        for row in rows
    ):
        raise signing.BadSignature("Invalid notification snapshot.")
    return rows


@transaction.atomic
def apply_snapshot(queryset, rows, *, dismiss=False):
    updated, now = 0, timezone.now()
    for pk, revision in rows:
        changes = {"dismissed_at": now} if dismiss else {"is_read": True, "read_at": now, "read_revision": revision}
        updated += queryset.filter(pk=pk, revision=revision).update(**changes)
    return updated
