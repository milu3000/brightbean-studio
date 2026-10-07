"""An explicit native read cannot mutate local history, drafts or permissions."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.inbox import native_thread_reads as reads
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
from apps.inbox.tests.test_shared_reply_safety import dm as dm  # noqa: F401
from apps.members.models import CustomRole, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def refresh_url(message, workspace=None):
    return reverse(
        "inbox:native_thread_refresh",
        kwargs={"workspace_id": (workspace or message.workspace).pk, "message_id": message.pk},
    )


def compact_native_page(dm, *, count=100, page=0, more=True):
    """A provider page with realistic long IDs, never a live platform response."""
    stamp = timezone.now().replace(microsecond=0) - timedelta(days=page + 1)
    rows = [
        {
            "id": f"synthetic-platform-page-{page}-message-{index:03}-" + "x" * 80,
            "message": "Synthetic message text " * 100,
            "from": {"id": "peer-1"},
            "to": {"data": [{"id": dm.account.account_platform_id}]},
            "created_time": (stamp - timedelta(seconds=index)).isoformat(),
        }
        for index in range(count)
    ]
    messages = {"data": rows}
    if more:
        messages["paging"] = {
            "next": f"https://graph.facebook.com/v25.0/conversation-1/messages?after=page-{page + 1}",
            "cursors": {"after": f"page-{page + 1}"},
        }
    return {
        "id": "conversation-1",
        "participants": {"data": [{"id": dm.account.account_platform_id}, {"id": "peer-1"}]},
        "messages": messages,
    }


@pytest.mark.parametrize("count", [35, 100])
def test_ui_subpages_preserve_all_provider_ids_before_advancing_within_budget(client, dm, settings, count):
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    client.force_login(dm.user)
    before = InboxMessage.objects.values().get(pk=dm.message.pk)
    pages = [compact_native_page(dm, count=count, page=page, more=page == 0) for page in (0, 1)]
    continuation, observed = None, set()
    chunks_per_page = (count + reads.PROVIDER_PAGE_LIMIT - 1) // reads.PROVIDER_PAGE_LIMIT
    for step in range(2 * chunks_per_page):
        page = step // chunks_per_page
        data = pages[page]
        with patch.object(reads, "_request_native_thread", return_value=data) as provider:
            response = client.post(refresh_url(dm.message), {"continuation": continuation} if continuation else {})
        result = response.json()
        assert response.status_code == 200 and result["status"] == "observed"
        ids = {item["platform_message_id"] for item in result["items"]}
        assert ids <= {row["id"] for row in data["messages"]["data"]}
        assert 1 <= len(result["items"]) <= 20 and not observed & ids
        observed.update(ids)
        assert result["coverage"]["requested_limit"] == 50
        assert result["coverage"]["provider_page_limit"] == reads.PROVIDER_PAGE_LIMIT == 20
        assert result["coverage"]["output_omitted_count"] == 0
        assert result["history_complete"] is False and result["persisted"] is False
        assert len(response.content) < reads.MAX_RESULT_BYTES == 30 * 1024
        assert "no-store" in response["Cache-Control"]
        provider.assert_called_once()
        assert provider.call_args.args[2] == 20
        assert provider.call_args.args[3] == (None if page == 0 else "page-1")
        continuation = result["older_continuation"]
        if step < 2 * chunks_per_page - 1:
            assert continuation
    assert observed == {row["id"] for data in pages for row in data["messages"]["data"]}
    assert len(observed) == 2 * count and continuation is None
    assert InboxMessage.objects.values().get(pk=dm.message.pk) == before
    assert not ConversationMessage.objects.exists() and not InboxReply.objects.exists()


@pytest.mark.parametrize("older", [False, True])
def test_ui_rejects_whole_provider_pages_above_100_without_advancing(client, dm, older):
    client.force_login(dm.user)
    options = {}
    if older:
        with patch.object(reads, "_request_native_thread", return_value=compact_native_page(dm, count=20)):
            first = client.post(refresh_url(dm.message)).json()
        options["continuation"] = first["older_continuation"]
        assert options["continuation"]
    with patch.object(
        reads, "_request_native_thread", return_value=compact_native_page(dm, count=101, page=int(older))
    ) as provider:
        response = client.post(refresh_url(dm.message), {**options, "limit": 1000})
    result = response.json()
    assert response.status_code == 200 and result["reason_code"] == "invalid_response"
    assert result["items"] == [] and result["older_continuation"] is None and result["page_key"] is None
    assert result["coverage"]["requested_limit"] == 50
    assert result["coverage"]["scanned_count"] == result["coverage"]["skipped_count"] == 101
    provider.assert_called_once()
    assert provider.call_args.args[2] == 20


def test_ui_validates_identity_past_the_provider_hint_before_returning_any_subpage(client, dm):
    client.force_login(dm.user)
    data = compact_native_page(dm)
    data["messages"]["data"][-1]["from"]["id"] = "foreign-peer"
    with patch.object(reads, "_request_native_thread", return_value=data):
        result = client.post(refresh_url(dm.message)).json()
    assert result["reason_code"] == "message_scope_unverified" and result["items"] == []
    assert result["older_continuation"] is None


def test_ui_same_page_refetch_rejects_changed_unseen_identity_without_advancing(client, dm):
    client.force_login(dm.user)
    data = compact_native_page(dm, count=35, more=False)
    with patch.object(reads, "_request_native_thread", return_value=data):
        first = client.post(refresh_url(dm.message)).json()
    assert first["status"] == "observed" and len(first["items"]) == 20
    assert first["older_continuation"], "Remaining same-page rows need no provider next cursor"
    data["messages"]["data"][-1]["id"] = "changed-unseen-last-row"
    with patch.object(reads, "_request_native_thread", return_value=data) as provider:
        response = client.post(refresh_url(dm.message), {"continuation": first["older_continuation"]})
    result = response.json()
    assert response.status_code == 200 and result["reason_code"] == "stale_page"
    assert result["items"] == [] and result["older_continuation"] is None and result["page_key"] is None
    assert "no-store" in response["Cache-Control"]
    provider.assert_called_once()
    assert provider.call_args.args[3] is None


def test_ui_limit_does_not_widen_explicit_reader_callers_or_their_continuations(client, dm):
    client.force_login(dm.user)
    authorization = reads.session_read_authorization(dm.user)
    with patch.object(reads, "_request_native_thread", return_value=compact_native_page(dm)):
        result = reads.read_native_thread(dm.message, authorization=authorization, limit=7)
    assert result["coverage"]["requested_limit"] == 7
    assert result["status"] == "observed" and len(result["items"]) == 7
    with patch.object(reads, "_request_native_thread", return_value=compact_native_page(dm, count=20)):
        first = reads.read_native_thread(dm.message, authorization=authorization, limit=7)
    assert first["older_continuation"]
    with patch.object(reads, "_request_native_thread") as provider:
        response = client.post(refresh_url(dm.message), {"continuation": first["older_continuation"]})
    assert response.status_code == 409 and response.json()["reason_code"] == "stale_continuation"
    assert "items" not in response.json()
    provider.assert_not_called()


@pytest.mark.parametrize("change", ["actor", "thread"])
def test_ui_subpage_continuation_still_pins_actor_and_thread(client, dm, change):
    client.force_login(dm.user)
    with patch.object(reads, "_request_native_thread", return_value=compact_native_page(dm, count=20)):
        first = client.post(refresh_url(dm.message)).json()
    if change == "actor":
        actor = type(dm.user).objects.create_user(
            email="other-reader@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        WorkspaceMembership.objects.create(user=actor, workspace=dm.account.workspace, workspace_role="owner")
        client.force_login(actor)
    else:
        dm.message.extra["conversation_id"] = "other-conversation"
        dm.message.save(update_fields=["extra"])
    with patch.object(reads, "_request_native_thread") as provider:
        response = client.post(refresh_url(dm.message), {"continuation": first["older_continuation"]})
    assert response.status_code == 409 and response.json()["reason_code"] == "stale_continuation"
    assert "items" not in response.json()
    provider.assert_not_called()


@pytest.mark.parametrize(
    ("change", "status", "code"), [("permission", 403, "authorization_revoked"), ("thread", 409, "stale")]
)
def test_ui_subpage_rechecks_current_authority_and_identity_after_provider(client, dm, change, status, code):
    client.force_login(dm.user)

    def changed_during_read(*args):
        if change == "permission":
            dm.member.delete()
        else:
            dm.message.extra["conversation_id"] = "changed-during-read"
            dm.message.save(update_fields=["extra"])
        return compact_native_page(dm)

    with patch.object(reads, "_request_native_thread", side_effect=changed_during_read) as provider:
        response = client.post(refresh_url(dm.message))
    assert response.status_code == status and response.json()["reason_code"] == code
    assert "items" not in response.json() and "no-store" in response["Cache-Control"]
    provider.assert_called_once()


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
