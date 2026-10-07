"""An explicit native read cannot mutate local history, drafts or permissions."""

from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import (
    ConversationMessage,
    ConversationSyncState,
    ConversationWorkState,
    DMConversationOwnership,
    DMSendAttempt,
    InboxConversation,
    InboxMessage,
    InboxReply,
    InternalNote,
    SendOperation,
)
from apps.inbox.native_thread_reads import NativeThreadReadError
from apps.inbox.tests.test_conversation_presentation import detail_url, incoming
from apps.inbox.tests.test_conversation_presentation import owner_client as owner_client  # noqa: F401
from apps.members.models import CustomRole, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def refresh_url(message, workspace=None):
    return reverse(
        "inbox:native_thread_refresh",
        kwargs={"workspace_id": (workspace or message.workspace).pk, "message_id": message.pk},
    )


@pytest.fixture
def native_read():
    with patch("apps.inbox.native_thread_reads.read_native_thread") as read:
        read.return_value = {
            "status": "observed",
            "reason_code": "bounded_snapshot",
            "checked_at": timezone.now().isoformat(),
            "platform": "facebook",
            "items": [],
            "history_complete": False,
            "persisted": False,
            "more_available": False,
            "newer_outbound_observed": False,
            "coverage": {"kind": "one_time_native_thread", "truncated": False},
        }
        yield read


def test_rendering_requires_a_separate_scoped_browser_read(owner_client, inbox_account, native_read):
    message = incoming(inbox_account, "selected")
    for url in (detail_url(message), reverse("inbox:feed", kwargs={"workspace_id": message.workspace_id})):
        response = owner_client.get(url)
        assert response.status_code == 200
        assert b"inbox-native-thread.js" in response.content
    html = owner_client.get(detail_url(message), HTTP_HX_REQUEST="true").content.decode()
    assert "Retry platform read" in html
    assert f'data-anchor-id="{message.pk}"' in html
    assert "data-native-thread-result hidden" in html
    assert "data-inbox-scroll" in html
    assert "Read latest platform conversation" not in html
    assert "Temporary platform snapshot" not in html
    assert "data-native-thread-refresh hx-" not in html
    native_read.assert_not_called()


@pytest.mark.parametrize("status", ["unread", "open", "archived", "resolved"])
def test_refresh_preserves_all_local_state_with_v2_off(
    owner_client, inbox_account, user, native_read, settings, status
):
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    message = incoming(inbox_account, "selected", status=status, assigned_to=user)
    latest = incoming(inbox_account, "latest", minutes=1)
    reply = InboxReply.objects.create(inbox_message=message, author=user, body="Saved draft", status="draft")
    InternalNote.objects.create(inbox_message=message, author=user, body="Internal note")
    models = (
        InboxMessage,
        InboxReply,
        InternalNote,
        InboxConversation,
        ConversationMessage,
        ConversationSyncState,
        ConversationWorkState,
        DMConversationOwnership,
        DMSendAttempt,
        SendOperation,
        SocialAccount,
    )
    before = {model: list(model.objects.order_by("pk").values()) for model in models}
    native_read.return_value.update(
        anchor_message_id=str(message.pk),
        newer_outbound_observed=True,
        items=[
            {
                "platform_message_id": "external-reply",
                "direction": "outbound",
                "source": "platform_observed",
                "body": "Answered on platform",
                "occurred_at": timezone.now().isoformat(),
                "attachments": [],
            }
        ],
    )
    with patch("apps.inbox.services.send_reply_now") as send, patch("apps.inbox.services.create_reply_draft") as draft:
        response = owner_client.post(
            refresh_url(message),
            {
                "body": "Injected new draft",
                "status": "resolved",
                "assigned_to": "",
                "limit": 1000,
                "follow_up_reply_id": str(reply.pk),
                "panel_message_id": str(latest.pk),
            },
        )
    assert response.status_code == 200
    assert response.json() == native_read.return_value
    assert "no-store" in response["Cache-Control"]
    assert not any(key.startswith("HX-") for key in response.headers)
    assert native_read.call_args.args[0].pk == message.pk
    assert native_read.call_args.kwargs["limit"] == 50
    native_read.call_args.kwargs["authorization"](inbox_account)
    assert before == {model: list(model.objects.order_by("pk").values()) for model in models}
    send.assert_not_called()
    draft.assert_not_called()


def test_refresh_requires_explicit_post_and_csrf(owner_client, inbox_account, user, native_read):
    message = incoming(inbox_account, "selected")
    for method in (owner_client.get, owner_client.head):
        response = method(refresh_url(message))
        assert response.status_code == 405
        assert "no-store" in response["Cache-Control"]
    secure = Client(enforce_csrf_checks=True)
    secure.force_login(user)
    assert secure.post(refresh_url(message)).status_code == 403
    native_read.assert_not_called()


def test_inbox_read_permission_suffices_without_send_or_admin_permissions(
    owner_client, inbox_account, user, organization, native_read
):
    message = incoming(inbox_account, "selected")
    role = CustomRole.objects.create(organization=organization, name="Read inbox", permissions={"use_inbox": True})
    WorkspaceMembership.objects.filter(user=user, workspace=inbox_account.workspace).update(custom_role=role)
    assert owner_client.post(refresh_url(message)).status_code == 200
    native_read.call_args.kwargs["authorization"](inbox_account)
    role.permissions = {"use_inbox": False}
    role.save(update_fields=["permissions"])
    native_read.reset_mock()
    assert owner_client.post(refresh_url(message)).status_code == 403
    native_read.assert_not_called()


def test_refresh_rejects_foreign_workspace_and_account(owner_client, inbox_account, organization, native_read):
    workspace = Workspace.objects.create(name="Foreign", organization=organization)
    account = SocialAccount.objects.create(workspace=workspace, platform="facebook", account_platform_id="foreign")
    foreign = incoming(account, "foreign")
    corrupt = incoming(account, "corrupt", workspace=inbox_account.workspace)
    assert owner_client.post(refresh_url(foreign, inbox_account.workspace)).status_code == 404
    assert owner_client.post(refresh_url(corrupt)).status_code == 404
    native_read.assert_not_called()


@pytest.mark.parametrize(("code", "status"), [("authorization_revoked", 403), ("stale", 409), ("invalid_limit", 400)])
def test_service_denial_never_returns_contents_or_changes_selection(
    owner_client, inbox_account, native_read, code, status
):
    message = incoming(inbox_account, "selected")
    native_read.side_effect = NativeThreadReadError(code, "Raw private diagnostic")
    response = owner_client.post(refresh_url(message))
    assert response.status_code == status
    assert response.json() == {"status": "unavailable", "reason_code": code, "anchor_message_id": str(message.pk)}
    assert "no-store" in response["Cache-Control"]
    assert b"Raw private" not in response.content
    message.refresh_from_db()
    assert message.status == "unread"


def test_unexpected_failure_is_content_free_and_not_logged(owner_client, inbox_account, native_read, caplog):
    message = incoming(inbox_account, "selected")
    native_read.side_effect = RuntimeError("PRIVATE RAW PROVIDER PAYLOAD")
    response = owner_client.post(refresh_url(message))
    assert response.status_code == 502
    assert "no-store" in response["Cache-Control"]
    assert b"PRIVATE RAW" not in response.content
    assert "PRIVATE RAW" not in caplog.text
    assert response.json()["reason_code"] == "read_failed"


@pytest.mark.parametrize("reason", ["missing_native_thread", "unverified_thread", "unsupported_platform"])
def test_unavailable_snapshot_is_explicit_and_content_free(owner_client, inbox_account, native_read, reason):
    message = incoming(inbox_account, "selected")
    native_read.return_value.update(status="unavailable", reason_code=reason, anchor_message_id=str(message.pk))
    response = owner_client.post(refresh_url(message))
    assert response.status_code == 200
    assert response.json()["reason_code"] == reason
    assert response.json()["items"] == []
    assert response.json()["history_complete"] is False


def test_scrolling_posts_only_the_selected_threads_continuation(owner_client, inbox_account, native_read):
    message = incoming(inbox_account, "selected")
    native_read.return_value["anchor_message_id"] = str(message.pk)
    response = owner_client.post(refresh_url(message), {"continuation": "synthetic-signed-position"})
    assert response.status_code == 200
    assert native_read.call_args.args[0].pk == message.pk
    assert native_read.call_args.kwargs["continuation"] == "synthetic-signed-position"
    assert native_read.call_args.kwargs["limit"] == 50
    assert "no-store" in response["Cache-Control"]


@pytest.mark.parametrize("code", ["invalid_continuation", "stale_continuation"])
def test_continuation_failure_carries_no_private_diagnostics(owner_client, inbox_account, native_read, code):
    message = incoming(inbox_account, "selected")
    native_read.side_effect = NativeThreadReadError(code, "private-provider-diagnostic")
    response = owner_client.post(refresh_url(message), {"continuation": "synthetic-position"})
    assert response.status_code == (400 if code == "invalid_continuation" else 409)
    assert response.json() == {"status": "unavailable", "reason_code": code, "anchor_message_id": str(message.pk)}
    assert "private-provider-diagnostic" not in response.content.decode()
    assert "no-store" in response["Cache-Control"]
