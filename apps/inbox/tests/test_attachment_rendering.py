"""The classic inbox shows attachment-only messages without unsafe markup."""

import uuid
from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string
from django.utils import timezone

from apps.inbox.models import InboxMessage
from apps.members.models import WorkspaceMembership


def _attachment(**overrides):
    return {
        "type": "share",
        "url": "https://www.instagram.com/p/shared/",
        "title": "Shared post",
        "preview_url": "",
        "availability": "available",
        **overrides,
    }


def _message(**overrides):
    return SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "sender_name": "Ada",
            "sender_handle": "ada",
            "sender_avatar_url": "",
            "body": "",
            "content_preview": "Shared content",
            "attachments": [_attachment()],
            "received_at": timezone.now(),
            "status": "unread",
            "sentiment": "neutral",
            "assigned_to": None,
            "social_account": SimpleNamespace(platform="instagram", account_name="Test account", account_handle=""),
            "get_message_type_display": "Direct Message",
            "get_status_display": "Unread",
            "get_sentiment_display": "Neutral",
            "extra": {"access_token": "private-provider-token"},
            **overrides,
        }
    )


def _render(template, message, **context):
    return render_to_string(
        f"inbox/partials/{template}.html",
        {"message": message, "workspace": SimpleNamespace(id=uuid.uuid4()), **context},
    )


@pytest.mark.parametrize("preview", ["Shared content", "Photo", "Video", "Audio", "File", "Non-text message"])
def test_message_row_uses_non_text_content_preview(preview):
    html = _render("_message_row", _message(content_preview=preview))

    assert f">{preview}</p>" in html
    assert "inbox-row" in html
    assert "private-provider-token" not in html


def test_share_only_detail_renders_safe_card_and_preserves_reply_composer():
    html = _render("_message_panel", _message())

    assert "Shared content" in html
    assert "Shared post" in html
    assert 'href="https://www.instagram.com/p/shared/"' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert "Open shared content" in html
    assert 'text-stone-800 leading-relaxed whitespace-pre-wrap"></p>' not in html
    assert "Save as draft" in html
    assert "Send Reply" in html
    assert "Internal Note" in html
    assert "private-provider-token" not in html


def test_mixed_detail_and_child_cards_escape_body_and_title():
    body = '<script>alert("body")</script>'
    title = '<img src=x onerror="alert(1)">'
    child = _message(sender_name="Child", attachments=[_attachment(title="Child share")])
    html = _render(
        "_message_panel",
        _message(body=body, content_preview=body, attachments=[_attachment(title=title)]),
        child_messages=[child],
    )

    assert "&lt;script&gt;alert(&quot;body&quot;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert body not in html
    assert title not in html
    assert "Child share" in html
    assert html.count("Open shared content") == 2


def test_missing_url_has_truthful_placeholder_and_no_link_or_image():
    html = _render("_attachment_cards", _message(attachments=[_attachment(url="", availability="unavailable")]))

    assert "Original content unavailable" in html
    assert "No usable link is available for this message" in html
    assert "<a " not in html
    assert "<img " not in html


def test_preview_is_lazy_and_does_not_send_referrer():
    url = "https://scontent.cdninstagram.com/preview.jpg"
    html = _render("_attachment_cards", _message(attachments=[_attachment(preview_url=url)]))

    assert f'src="{url}"' in html
    assert 'loading="lazy" referrerpolicy="no-referrer"' in html
    assert "<iframe" not in html
    assert "<video" not in html


def test_empty_legacy_message_shows_unknown_content_without_inventing_attachment():
    html = _render("_message_panel", _message(attachments=[], content_preview="Non-text message"))

    assert "Non-text message" in html
    assert "No displayable text or attachment metadata was provided for this message" in html
    assert "its original content has not been verified" in html
    assert "Open attachment" not in html


def test_text_only_message_keeps_body_without_empty_attachment_section():
    html = _render("_message_panel", _message(body="Plain text", content_preview="Plain text", attachments=[]))

    assert "Plain text" in html
    assert 'class="inbox-attachments' not in html
    assert "unavailable" not in html


@pytest.fixture
def inbox_owner_client(client, inbox_workspace, org_owner):
    WorkspaceMembership.objects.create(
        user=org_owner, workspace=inbox_workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    client.force_login(org_owner)
    return client


@pytest.mark.django_db
@pytest.mark.parametrize("htmx", [False, True])
def test_detail_view_renders_normalized_parent_and_child_shares(inbox_owner_client, inbox_message, htmx):
    inbox_message.body = ""
    inbox_message.extra = {
        "message": {
            "attachments": [{"type": "share", "payload": {"url": "https://www.instagram.com/p/parent-share/"}}]
        },
        "access_token": "private-provider-token",
    }
    inbox_message.save(update_fields=["body", "extra"])
    InboxMessage.objects.create(
        workspace=inbox_message.workspace,
        social_account=inbox_message.social_account,
        parent_message=inbox_message,
        platform_message_id="child-share",
        message_type="dm",
        sender_name="Child sender",
        body="",
        extra={"inbox_attachments": [_attachment(url="https://www.instagram.com/p/child-share/")]},
        received_at=timezone.now(),
    )

    response = inbox_owner_client.get(
        f"/workspace/{inbox_message.workspace_id}/inbox/{inbox_message.id}/",
        **({"HTTP_HX_REQUEST": "true"} if htmx else {}),
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert 'href="https://www.instagram.com/p/parent-share/"' in html
    assert 'href="https://www.instagram.com/p/child-share/"' in html
    assert html.count("Open shared content") == 2
    assert "private-provider-token" not in html
    assert "Save as draft" in html
    inbox_message.refresh_from_db()
    assert inbox_message.status == InboxMessage.Status.OPEN


@pytest.mark.django_db
def test_feed_keeps_individual_rows_and_nonempty_attachment_preview(inbox_owner_client, inbox_message):
    inbox_message.body = ""
    inbox_message.extra = {"inbox_attachments": [_attachment()]}
    inbox_message.save(update_fields=["body", "extra"])
    second = InboxMessage.objects.create(
        workspace=inbox_message.workspace,
        social_account=inbox_message.social_account,
        platform_message_id="second-share",
        message_type="dm",
        sender_name=inbox_message.sender_name,
        body="",
        extra={"inbox_attachments": [_attachment()]},
        received_at=timezone.now(),
    )

    response = inbox_owner_client.get(f"/workspace/{inbox_message.workspace_id}/inbox/", HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    html = response.content.decode()
    assert f'id="msg-{inbox_message.id}"' in html
    assert f'id="msg-{second.id}"' in html
    assert html.count(">Shared content</p>") == 2
