"""Additional replies are an explicit composer intent, never a retry fallback."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.dm_send_gate import DMSendGateError
from apps.inbox.models import InboxReply
from apps.inbox.tests.test_conversation_presentation import detail_url, incoming
from apps.inbox.tests.test_conversation_presentation import owner_client as owner_client  # noqa: F401
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def sent_parent(message, user=None, **overrides):
    return InboxReply.objects.create(
        **{
            "inbox_message": message,
            "author": user,
            "body": "Our confirmed earlier answer",
            "status": "sent",
            "sent_at": timezone.now() - timedelta(minutes=2),
            "platform_reply_id": f"receipt-{message.platform_message_id}",
            **overrides,
        }
    )


def reply_url(message, action="send_reply"):
    return reverse(
        action if ":" in action else f"inbox:{action}",
        kwargs={"workspace_id": message.workspace_id, "message_id": message.pk},
    )


def test_visible_sent_receipt_selects_exact_parent_in_one_composer(owner_client, inbox_account):
    original = incoming(inbox_account, "original", minutes=-10)
    parent = sent_parent(original)
    latest = incoming(inbox_account, "newest", minutes=-1)
    normal = owner_client.get(detail_url(original), HTTP_HX_REQUEST="true")
    assert normal.context["reply_target"].pk == latest.pk
    assert b"Send another reply" in normal.content
    assert b'name="follow_up_reply_id"' not in normal.content

    selected = owner_client.get(detail_url(original), {"follow_up_reply_id": str(parent.pk)}, HTTP_HX_REQUEST="true")
    html = selected.content.decode()
    assert selected.context["reply_target"].pk == original.pk
    assert selected.context["follow_up_parent"].pk == parent.pk
    assert selected.context["send_availability"]["allowed"] is True
    assert html.count('id="inbox-reply-body"') == 1
    assert f'name="follow_up_reply_id" value="{parent.pk}"' in html
    assert "Send additional reply" in html and "Cancel additional reply" in html


def test_save_explicit_follow_up_creates_child_with_same_original_target(owner_client, inbox_account, user):
    original = incoming(inbox_account, "original", minutes=-10)
    parent = sent_parent(original, user)
    incoming(inbox_account, "newer", minutes=-1)
    response = owner_client.post(
        reply_url(original, "save_reply_draft"),
        {"body": "One more detail", "follow_up_reply_id": str(parent.pk)},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    child = InboxReply.objects.get(follow_up_of=parent)
    assert child.inbox_message_id == original.pk and child.body == "One more detail"
    assert child.status == "draft"
    assert b"Additional reply after:" in response.content
    assert response.context["follow_up_reply_id"] == ""


def test_send_explicit_follow_up_uses_common_service_with_pinned_parent(owner_client, inbox_account):
    original = incoming(inbox_account, "original", minutes=-10)
    parent = sent_parent(original)
    incoming(inbox_account, "newer", minutes=-1)
    with patch("apps.inbox.services.send_reply") as send:
        response = owner_client.post(
            reply_url(original),
            {"body": "One more detail", "follow_up_reply_id": str(parent.pk)},
            HTTP_HX_REQUEST="true",
        )
    assert response.status_code == 200
    assert send.call_args.kwargs["message"].pk == original.pk
    assert send.call_args.kwargs["follow_up_of"].pk == parent.pk
    assert response.context["follow_up_reply_id"] == ""


@pytest.mark.parametrize("action", ["send_reply", "save_reply_draft"])
def test_failure_preserves_body_target_and_explicit_mode_when_new_message_arrives(owner_client, inbox_account, action):
    original = incoming(inbox_account, "original", minutes=-10)
    parent = sent_parent(original)
    latest = incoming(inbox_account, "newer", minutes=-1)
    service = "send_reply" if action == "send_reply" else "create_reply_draft"
    with patch(f"apps.inbox.services.{service}", side_effect=DMSendGateError("held", "Review before sending.")):
        response = owner_client.post(
            reply_url(original, action),
            {"body": "Keep this extra detail", "follow_up_reply_id": str(parent.pk)},
            HTTP_HX_REQUEST="true",
        )
    assert response.status_code == 200 and response["HX-Reply-Failed"] == "1"
    assert response.context["composer_body"] == "Keep this extra detail"
    assert response.context["reply_target"].pk == original.pk != latest.pk
    assert response.context["follow_up_reply_id"] == str(parent.pk)
    assert f'name="follow_up_reply_id" value="{parent.pk}"' in response.content.decode()


@pytest.mark.parametrize("status", ["unknown", "failed", "draft"])
def test_non_sent_parent_holds_without_converting_to_default_send(owner_client, inbox_account, status):
    original = incoming(inbox_account, "original", minutes=-10)
    parent = sent_parent(original, status=status)
    with patch("apps.inbox.services.send_reply") as send:
        response = owner_client.post(
            reply_url(original),
            {"body": "Do not retarget", "follow_up_reply_id": str(parent.pk)},
            HTTP_HX_REQUEST="true",
        )
    send.assert_not_called()
    assert response["HX-Reply-Failed"] == "1"
    assert response.context["send_availability"]["allowed"] is False
    assert response.context["follow_up_reply_id"] == str(parent.pk)
    assert response.context["composer_body"] == "Do not retarget"


@pytest.mark.parametrize("foreign_scope", ["message", "thread", "account", "workspace"])
def test_foreign_parent_never_changes_the_destination(owner_client, inbox_account, organization, foreign_scope):
    original = incoming(inbox_account, "original", minutes=-10)
    account = inbox_account
    if foreign_scope in {"account", "workspace"}:
        workspace = (
            inbox_account.workspace
            if foreign_scope == "account"
            else Workspace.objects.create(name="Foreign", organization=organization)
        )
        account = SocialAccount.objects.create(
            workspace=workspace, platform="facebook", account_platform_id="other-page"
        )
    foreign = incoming(
        account, "foreign", minutes=-5, native="other-thread" if foreign_scope == "thread" else "thread-a"
    )
    parent = sent_parent(foreign, body="Foreign private answer")
    with patch("apps.inbox.services.send_reply") as send:
        response = owner_client.post(
            reply_url(original),
            {"body": "My unsaved reply", "follow_up_reply_id": str(parent.pk)},
            HTTP_HX_REQUEST="true",
        )
    send.assert_not_called()
    assert response.context["send_availability"]["allowed"] is False
    assert response.context["follow_up_parent"] is None
    if foreign_scope != "message":
        assert b"Foreign private answer" not in response.content
    assert response.context["reply_target"].pk == original.pk


def test_invalid_parent_identifier_is_held_and_escaped(owner_client, inbox_account):
    message = incoming(inbox_account, "original", minutes=-10)
    parent_id = '<script>alert("bad")</script>'
    response = owner_client.post(
        reply_url(message), {"body": "Keep text", "follow_up_reply_id": parent_id}, HTTP_HX_REQUEST="true"
    )
    assert response.context["send_availability"]["allowed"] is False
    assert parent_id not in response.content.decode()
    assert response.context["follow_up_reply_id"] == parent_id


def test_invalid_mode_never_treats_an_ordinary_draft_as_the_saved_follow_up(owner_client, inbox_account):
    message = incoming(inbox_account, "original", minutes=-10)
    InboxReply.objects.create(inbox_message=message, body="Same text", status="draft")
    response = owner_client.post(
        reply_url(message), {"body": "Same text", "follow_up_reply_id": "invalid"}, HTTP_HX_REQUEST="true"
    )
    assert response.context["composer_body"] == "Same text"
    assert response.context["follow_up_reply_id"] == "invalid"
    assert response.context["send_availability"]["allowed"] is False


def test_orphaned_additional_draft_stays_visible_and_cannot_be_repurposed(owner_client, inbox_account):
    message = incoming(inbox_account, "original", minutes=-10)
    child = InboxReply.objects.create(
        inbox_message=message, body="Preserve this additional reply", status="draft", is_follow_up=True
    )
    response = owner_client.get(detail_url(message), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert "Preserve this additional reply" in html and "original sent reply unavailable" in html
    assert f"replies/{child.pk}/edit/" not in html
    assert f"replies/{child.pk}/discard/" not in html
    assert response.context["draft_replies"][0].send_availability["allowed"] is False


def test_parent_without_provider_confirmation_does_not_enable_follow_up(owner_client, inbox_account):
    message = incoming(inbox_account, "original", minutes=-10)
    parent = sent_parent(message, platform_reply_id="")
    response = owner_client.get(detail_url(message), {"follow_up_reply_id": str(parent.pk)}, HTTP_HX_REQUEST="true")
    assert response.context["send_availability"]["allowed"] is False
    assert response.context["can_save_draft"] is False


def test_existing_follow_up_draft_is_reviewed_instead_of_duplicated(owner_client, inbox_account, user):
    from apps.inbox.services import create_reply_draft

    message = incoming(inbox_account, "original", minutes=-10)
    parent = sent_parent(message, user)
    child = create_reply_draft(message=message, body="Already saved", author=user, follow_up_of=parent)
    response = owner_client.get(detail_url(message), {"follow_up_reply_id": str(parent.pk)}, HTTP_HX_REQUEST="true")
    assert response.context["send_availability"]["allowed"] is False
    assert response.context["send_availability"]["existing_reply_id"] == str(child.pk)
    assert "Review it in Pending replies" in response.content.decode()
    assert InboxReply.objects.filter(follow_up_of=parent).count() == 1


@pytest.mark.parametrize("child_status", ["sent", "unknown"])
def test_consumed_parent_is_held_without_automatically_selecting_its_child(owner_client, inbox_account, child_status):
    message = incoming(inbox_account, "original", minutes=-10)
    parent = sent_parent(message)
    child = sent_parent(
        message, follow_up_of=parent, is_follow_up=True, status=child_status, platform_reply_id="second-receipt"
    )
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = owner_client.post(
            reply_url(message),
            {"body": "Do not create a third reply", "follow_up_reply_id": str(parent.pk)},
            HTTP_HX_REQUEST="true",
        )
    provider.assert_not_called()
    assert response["HX-Reply-Failed"] == "1"
    assert b"already has a follow-up" in response.content
    assert response.context["follow_up_reply_id"] == str(parent.pk) != str(child.pk)
    assert response.context["send_availability"]["allowed"] is False
    assert InboxReply.objects.filter(inbox_message=message).count() == 2


def test_presentation_fallback_uses_same_explicit_follow_up_controls(owner_client, inbox_account, settings):
    settings.INBOX_CONVERSATION_PRESENTATION_ENABLED = False
    message = incoming(inbox_account, "original", minutes=-10)
    parent = sent_parent(message)
    response = owner_client.get(detail_url(message), {"follow_up_reply_id": str(parent.pk)}, HTTP_HX_REQUEST="true")
    assert response.context["conversation_view"] is False
    assert response.context["send_availability"]["allowed"] is True
    assert response.content.decode().count('id="inbox-reply-body"') == 1
    assert "Send additional reply" in response.content.decode()
