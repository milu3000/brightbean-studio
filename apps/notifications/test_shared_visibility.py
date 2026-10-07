"""Integration gates activated when the rebuilt canonical modules are present."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.apps import apps
from django.utils import timezone

from apps.inbox.models import ConversationMessage, InboxConversation, InboxMessage
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .models import EventType, Notification

pytestmark = pytest.mark.django_db


@pytest.fixture
def source(user, organization):
    try:
        from apps.inbox.canonical_content import visible_content
    except ModuleNotFoundError as exc:
        if exc.name != "apps.inbox.canonical_content":
            raise
        pytest.skip("Requires the rebuilt shared canonical policy in combined integration")
    state_model = apps.get_model("inbox", "ConversationObservationState")
    connection_model = apps.get_model("inbox", "InboxSyncConnection")
    identity_model = apps.get_model("inbox", "ConversationSyncIdentity")
    workspace = Workspace.objects.create(name="Visibility integration", organization=organization)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="facebook", account_platform_id="old-page", account_name="Page"
    )
    legacy = InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id="source",
        message_type="dm",
        sender_name="Peer",
        body="Legacy private body",
        received_at=timezone.now(),
    )
    conversation = InboxConversation.objects.create(
        workspace=workspace,
        social_account=account,
        platform="facebook",
        platform_conversation_id="thread",
        peer_id="peer",
    )
    row = ConversationMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform="facebook",
        conversation=conversation,
        platform_message_id="source",
        direction="inbound",
        sender_id="peer",
        recipient_id="old-page",
        body="Canonical private body",
        legacy_message=legacy,
    )
    conn = connection_model.objects.create(
        workspace=workspace,
        social_account=account,
        platform="facebook",
        account_platform_id="old-page",
        auth_fingerprint="synthetic",
        enabled=True,
    )
    identity_model.objects.create(conversation=conversation, connection=conn, connection_generation=conn.generation)
    state = state_model.objects.create(
        message=row,
        connection_generation=conn.generation,
        last_observed_at=timezone.now(),
        expires_at=timezone.now() + timedelta(days=1),
        retained_body="Restricted archive must never appear",
    )
    return workspace, account, legacy, row, conversation, conn, state, visible_content


@pytest.mark.parametrize("link", ["canonical", "legacy_fk", "legacy_json"])
@pytest.mark.parametrize("restriction", ["expired", "withdrawn", "identity_rebind", "generation_rebind"])
def test_default_preview_obeys_current_shared_policy(user, source, link, restriction):
    workspace, account, legacy, row, conversation, conn, state, visible_content = source
    data = {"workspace_id": str(workspace.pk)}
    kwargs = {}
    if link == "canonical":
        data["canonical_message_id"] = str(row.pk)
        kwargs["conversation"] = conversation
    elif link == "legacy_fk":
        kwargs["inbox_message"] = legacy
    else:
        data["message_id"] = str(legacy.pk)
    notification = Notification.objects.create(
        user=user,
        workspace=workspace,
        event_type=EventType.NEW_INBOX_MESSAGE,
        title="New",
        body="Denormalized private body",
        data=data,
        **kwargs,
    )
    if restriction == "expired":
        state.expired_at = timezone.now()
        state.save(update_fields=["expired_at"])
    elif restriction == "withdrawn":
        state.withdrawn_at = timezone.now()
        state.save(update_fields=["withdrawn_at"])
    elif restriction == "identity_rebind":
        account.account_platform_id = "new-page"
        account.save(update_fields=["account_platform_id"])
    else:
        conn.generation = uuid4()
        conn.save(update_fields=["generation"])
    assert visible_content(row)["body"] == ""
    assert notification.display_body == ""


def test_planned_retention_deadline_does_not_change_notification_visibility(user, source):
    workspace, account, legacy, row, conversation, conn, state, visible_content = source
    state.expires_at = timezone.now() - timedelta(seconds=1)
    state.save(update_fields=["expires_at"])
    notification = Notification.objects.create(
        user=user,
        workspace=workspace,
        inbox_message=legacy,
        event_type=EventType.NEW_INBOX_MESSAGE,
        title="New",
        body="Old cached copy",
    )
    assert visible_content(row)["body"] == "Canonical private body"
    assert notification.display_body == "Canonical private body"


@pytest.mark.parametrize("link", ["legacy_fk", "legacy_json"])
def test_unlinked_canonical_shadow_survives_platform_rebind(user, source, link):
    workspace, account, legacy, row, conversation, conn, state, visible_content = source
    row.legacy_message = None
    row.save(update_fields=["legacy_message"])
    account.platform = "instagram_login"
    account.save(update_fields=["platform"])
    notification = Notification.objects.create(
        user=user,
        workspace=workspace,
        event_type=EventType.NEW_INBOX_MESSAGE,
        title="New",
        body="Old cached copy",
        inbox_message=legacy if link == "legacy_fk" else None,
        data={"message_id": str(legacy.pk), "workspace_id": str(workspace.pk)} if link == "legacy_json" else {},
    )
    assert notification.display_body == ""


@pytest.mark.parametrize(
    "extra",
    [
        {"is_deleted": True},
        {"message": {"is_deleted": True}},
        {"content_status": "expired"},
        {"inbox_content_status": "expired"},
    ],
)
def test_unshadowed_legacy_applied_restriction_never_uses_raw_body(user, source, extra):
    workspace, account, *_ = source
    legacy = InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id="unshadowed",
        message_type="dm",
        sender_name="Synthetic",
        body="Preserved raw text",
        extra=extra,
        received_at=timezone.now(),
    )
    notice = Notification.objects.create(
        user=user,
        workspace=workspace,
        inbox_message=legacy,
        event_type=EventType.NEW_INBOX_MESSAGE,
        title="New",
        body="Old preview",
    )
    assert notice.display_body == ""
    assert InboxMessage.objects.get(pk=legacy.pk).body == "Preserved raw text"
