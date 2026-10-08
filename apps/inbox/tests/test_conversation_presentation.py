"""Stored inbox history stays scoped, truthful and actionable in one view."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox import presentation
from apps.inbox.models import ConversationMessage, InboxConversation, InboxMessage, InboxReply, InternalNote
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(client, inbox_workspace, org_owner):
    WorkspaceMembership.objects.create(
        user=org_owner, workspace=inbox_workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    client.force_login(org_owner)
    return client


def incoming(account, key, *, native="thread-a", minutes=0, body=None, kind="dm", **overrides):
    extra = {
        "conversation_id": native,
        "sender_id": "peer-a",
        "participant_ids": [account.account_platform_id, "peer-a"],
    }
    return InboxMessage.objects.create(
        **{
            "workspace": account.workspace,
            "social_account": account,
            "platform_message_id": key,
            "message_type": kind,
            "sender_name": "Ada",
            "sender_handle": "peer-a",
            "extra": extra,
            "body": body if body is not None else f"Incoming {key}",
            "received_at": timezone.now() + timedelta(minutes=minutes),
            **overrides,
        }
    )


def detail_url(message):
    return reverse("inbox:message_detail", kwargs={"workspace_id": message.workspace_id, "message_id": message.pk})


def feed_url(workspace):
    return reverse("inbox:feed", kwargs={"workspace_id": workspace.pk})


@pytest.mark.parametrize("native", [None, "", 123, True, {}, ["thread"], " space", "tab\tid", "x\x01", "x" * 256])
def test_missing_or_malformed_native_ids_never_group_by_sender(inbox_account, native):
    first = incoming(inbox_account, "first", native=native)
    incoming(inbox_account, "second", native=native)

    assert list(presentation.stored_thread_messages(first)) == [first]
    page = presentation.inbox_page(InboxMessage.objects.all(), 1)
    assert len(page) == 2


def test_nul_native_id_is_rejected_before_database_storage():
    # PostgreSQL JSONB cannot store NUL. Keep this parser edge case separate
    # from the database grouping contract, which uses persistable controls.
    assert presentation.native_thread_id("x\x00") == ""


@pytest.mark.parametrize("native", ["123", "true", "null", "opaque:thread/abc"])
def test_opaque_native_string_ids_keep_their_exact_type(inbox_account, native):
    first = incoming(inbox_account, "first", native=native)
    second = incoming(inbox_account, "second", native=native)
    page = presentation.inbox_page(InboxMessage.objects.all(), 1)
    assert len(page) == 1
    assert page.object_list[0].matched_count == 2
    assert set(presentation.stored_thread_messages(first)) == {first, second}


@pytest.mark.parametrize(("native", "malformed"), [("123", 123), ("true", True), ("null", None)])
def test_native_string_does_not_match_a_different_json_type(inbox_account, native, malformed):
    selected = incoming(inbox_account, "valid", native=native)
    incoming(inbox_account, "malformed", native=malformed)
    assert list(presentation.stored_thread_messages(selected)) == [selected]
    assert len(presentation.inbox_page(InboxMessage.objects.all(), 1)) == 2


def test_native_threads_are_exact_account_and_workspace_scoped(inbox_account, organization):
    first = incoming(inbox_account, "first")
    second = incoming(inbox_account, "second")
    other_account = SocialAccount.objects.create(
        workspace=inbox_account.workspace, platform="facebook", account_platform_id="another-page"
    )
    incoming(other_account, "same-native-other-account")
    other_workspace = Workspace.objects.create(name="Private", organization=organization)
    foreign_account = SocialAccount.objects.create(
        workspace=other_workspace, platform="facebook", account_platform_id="foreign-page"
    )
    incoming(foreign_account, "same-native-foreign-workspace")
    incoming(inbox_account, "corrupt-workspace", workspace=other_workspace)
    incoming(inbox_account, "other-thread", native="thread-b")

    assert set(presentation.stored_thread_messages(first)) == {first, second}
    assert InboxConversation.objects.count() == ConversationMessage.objects.count() == 0


def test_group_and_unknown_threads_use_native_id_without_invented_names(inbox_account):
    a = incoming(
        inbox_account,
        "group-a",
        extra={"conversation_id": "group-a", "participant_ids": ["page-1", "peer-a", "b"], "sender_id": "peer-a"},
    )
    b = incoming(
        inbox_account,
        "group-b",
        extra={"conversation_id": "group-b", "participant_ids": ["page-1", "peer-a", "b"], "sender_id": "peer-a"},
    )
    unknown = incoming(inbox_account, "unknown", extra={"conversation_id": "known-native", "sender_id": "same-peer"})
    same_unknown = incoming(inbox_account, "unknown2", extra={"conversation_id": "known-native"})
    assert list(presentation.stored_thread_messages(a)) == [a]
    assert list(presentation.stored_thread_messages(b)) == [b]
    assert set(presentation.stored_thread_messages(unknown)) == {unknown, same_unknown}
    assert len(presentation.inbox_page(InboxMessage.objects.all(), 1)) == 3


@pytest.mark.parametrize("kind", ["comment", "mention", "review"])
def test_non_dm_messages_keep_individual_thread_behavior(inbox_account, kind):
    first = incoming(inbox_account, "first", kind=kind)
    incoming(inbox_account, "second", kind=kind)
    assert list(presentation.stored_thread_messages(first)) == [first]


def test_grouping_precedes_pagination_with_deterministic_representatives(inbox_account):
    stamp = timezone.now()
    duplicates = [incoming(inbox_account, f"same-{index}", received_at=stamp) for index in range(55)]
    for index in range(51):
        incoming(inbox_account, f"distinct-{index}", native=f"distinct-{index}", minutes=-index - 1)
    first = presentation.inbox_page(InboxMessage.objects.all(), 1)
    second = presentation.inbox_page(InboxMessage.objects.all(), 2)
    assert first.paginator.count == 52
    assert len(first) == 50 and len(second) == 2
    assert first.object_list[0].message_id == max(message.pk for message in duplicates)
    assert first.object_list[0].matched_count == 55
    assert not ({row.message_id for row in first} & {row.message_id for row in second})


def test_filtered_rows_report_matching_status_and_selection_only(inbox_account):
    old = incoming(inbox_account, "old", minutes=-10, body="needs help", status="unread")
    incoming(inbox_account, "latest", body="all done", status="resolved")
    page = presentation.inbox_page(InboxMessage.objects.filter(body__icontains="help"), 1)
    row = page.object_list[0]
    assert row.message_id == old.pk
    assert (row.matched_count, row.unread_count, row.open_count) == (1, 1, 0)


def test_timeline_pages_merge_sent_replies_notes_and_incoming_only(inbox_account, user):
    first = incoming(inbox_account, "old", minutes=-20)
    latest = incoming(inbox_account, "latest", minutes=-5)
    sent = InboxReply.objects.create(
        inbox_message=first,
        author=user,
        body="Delivered",
        status="sent",
        sent_at=timezone.now() - timedelta(minutes=10),
    )
    note = InternalNote.objects.create(inbox_message=latest, author=user, body="Team note")
    InboxReply.objects.create(inbox_message=first, body="Uncertain", status="unknown")
    InboxReply.objects.create(inbox_message=latest, body="Not sent", status="draft")
    qs = presentation.stored_thread_messages(first)
    newest = presentation.timeline_page(qs, 1, per_page=2)
    older = presentation.timeline_page(qs, 2, per_page=2)
    assert [(kind, item.pk) for kind, item, _ in newest] == [("incoming", latest.pk), ("note", note.pk)]
    assert [(kind, item.pk) for kind, item, _ in older] == [("incoming", first.pk), ("reply", sent.pk)]
    assert newest.paginator.count == 4


@pytest.mark.parametrize("htmx", [False, True])
def test_detail_shows_history_but_marks_only_selected_message_read(owner_client, inbox_account, user, htmx):
    selected = incoming(inbox_account, "selected", minutes=-20)
    latest = incoming(inbox_account, "latest", minutes=-5)
    sent = InboxReply.objects.create(
        inbox_message=selected, author=user, body="Our earlier answer", status="sent", sent_at=timezone.now()
    )
    response = owner_client.get(detail_url(selected), **({"HTTP_HX_REQUEST": "true"} if htmx else {}))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Incoming selected" in html and "Incoming latest" in html and sent.body in html
    assert f'data-reply-target-id="{latest.pk}"' in html
    assert f'data-selected-message-id="{selected.pk}"' in html
    selected.refresh_from_db()
    latest.refresh_from_db()
    assert selected.status == "open" and latest.status == "unread"
    assert "Status and assignment above apply only" not in html
    assert InboxConversation.objects.count() == ConversationMessage.objects.count() == 0


def test_timeline_htmx_page_preserves_composer_and_has_no_foreign_history(owner_client, inbox_account):
    selected = incoming(inbox_account, "selected", minutes=-100)
    for index in range(51):
        incoming(inbox_account, f"later-{index}", minutes=-index)
    other = SocialAccount.objects.create(
        workspace=inbox_account.workspace, platform="facebook", account_platform_id="foreign"
    )
    incoming(other, "private", body="Foreign private content")
    response = owner_client.get(
        detail_url(selected), {"history_page": 2}, HTTP_HX_REQUEST="true", HTTP_HX_TARGET="inbox-thread"
    )
    html = response.content.decode()
    assert response.status_code == 200
    assert "Incoming selected" in html and "Newer messages" in html
    assert "Foreign private content" not in html
    assert "inbox-reply-body" not in html and "data-inbox-panel" not in html


def test_older_draft_keeps_its_original_target_and_is_editable(owner_client, inbox_account, user):
    old = incoming(inbox_account, "old", minutes=-10)
    latest = incoming(inbox_account, "latest", minutes=-1)
    draft = InboxReply.objects.create(inbox_message=old, author=user, body="Earlier draft")
    response = owner_client.get(detail_url(latest), HTTP_HX_REQUEST="true")
    assert f'data-draft-target-id="{old.pk}"' in response.content.decode()
    assert f'data-reply-target-id="{latest.pk}"' in response.content.decode()
    url = reverse("inbox:update_reply_draft", kwargs={"workspace_id": old.workspace_id, "reply_id": draft.pk})
    response = owner_client.post(url, {"body": "Edited body", "panel_message_id": str(latest.pk)})
    draft.refresh_from_db()
    assert response.status_code == 200
    assert draft.inbox_message_id == old.pk and draft.body == "Edited body"
    assert f'data-selected-message-id="{latest.pk}"' in response.content.decode()


def test_sending_uses_explicit_url_target_even_after_new_message_arrives(owner_client, inbox_account):
    target = incoming(inbox_account, "displayed-target", minutes=-10)
    owner_client.get(detail_url(target))
    incoming(inbox_account, "new-arrival")
    url = reverse("inbox:send_reply", kwargs={"workspace_id": target.workspace_id, "message_id": target.pk})
    with patch("apps.inbox.services.send_reply") as send:
        response = owner_client.post(url, {"body": "Pinned answer"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert send.call_args.kwargs["message"].pk == target.pk
    assert response["HX-Retarget"] == "#inbox-detail-panel"


def test_unsaved_text_keeps_original_target_when_failure_and_new_incoming_overlap(owner_client, inbox_account):
    target = incoming(inbox_account, "original", minutes=-10)
    latest = incoming(inbox_account, "new-arrival")
    url = reverse("inbox:send_reply", kwargs={"workspace_id": target.workspace_id, "message_id": target.pk})
    with patch("apps.inbox.services.send_reply", side_effect=ValueError("Rejected before draft creation")):
        response = owner_client.post(url, {"body": "Keep addressed to original"}, HTTP_HX_REQUEST="true")
    assert response.context["reply_target"].pk == target.pk
    assert response.context["reply_target"].pk != latest.pk
    assert response.context["composer_body"] == "Keep addressed to original"
    assert response["HX-Reply-Failed"] == "1"


def test_selected_message_is_visible_even_when_older_than_first_history_page(owner_client, inbox_account):
    old = incoming(inbox_account, "old", minutes=-100)
    for index in range(51):
        incoming(inbox_account, f"later-{index}", minutes=-index)
    response = owner_client.get(detail_url(old), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert response.context["selected_outside_history"] is True
    assert html.count(f'id="incoming-{old.pk}"') == 1
    assert "Selected message · outside this history page" in html


def test_group_composer_is_held_but_draft_remains_available(owner_client, inbox_account):
    group = incoming(
        inbox_account,
        "group",
        extra={"conversation_id": "group", "participant_ids": ["page-1", "peer-a", "b"], "sender_id": "peer-a"},
    )
    response = owner_client.get(detail_url(group), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert response.status_code == 200
    assert "Group Message" in html and "verified one-to-one" in html
    assert 'disabled aria-describedby="reply-hold-reason"' in html
    assert response.context["can_save_draft"] is True


def test_fallback_changes_layout_without_changing_send_holds(owner_client, inbox_account, settings):
    selected = incoming(inbox_account, "selected", extra={"conversation_id": "a"})
    newer = incoming(inbox_account, "newer", extra={"conversation_id": "a"})
    settings.INBOX_CONVERSATION_PRESENTATION_ENABLED = False
    response = owner_client.get(detail_url(selected), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert "Incoming selected" in html and "Incoming newer" not in html
    assert 'disabled aria-describedby="reply-hold-reason"' in html
    assert set(presentation.stored_thread_messages(selected)) == {selected, newer}
    assert len(presentation.inbox_page(InboxMessage.objects.all(), 1)) == 2


def test_empty_filter_options_do_not_remove_all_rows(owner_client, inbox_account):
    message = incoming(inbox_account, "visible")
    response = owner_client.get(
        feed_url(inbox_account.workspace),
        {"platform": "", "type": "", "status": "", "sentiment": ""},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert f'id="msg-{message.pk}"' in response.content.decode()


@pytest.mark.parametrize("filters", [{"account": "bad-id"}, {"assigned": "bad-id"}, {"date_from": "2026-99-99"}])
def test_invalid_filters_fail_closed_without_server_error(owner_client, inbox_account, filters):
    incoming(inbox_account, "visible")
    response = owner_client.get(feed_url(inbox_account.workspace), filters, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert response.context["inbox_page"].paginator.count == 0


def test_unknown_outcome_is_pending_without_retry_edit_or_discard(owner_client, inbox_account):
    message = incoming(inbox_account, "unknown-outcome")
    reply = InboxReply.objects.create(inbox_message=message, status="unknown", body="May have arrived")
    response = owner_client.get(detail_url(message), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert "Outcome unknown" in html and "May have arrived" in html
    assert f'id="reply-{reply.pk}"' not in html
    assert f"replies/{reply.pk}/send/" not in html
    assert f"replies/{reply.pk}/edit/" not in html
    assert f"replies/{reply.pk}/discard/" not in html
    assert 'disabled aria-describedby="reply-hold-reason"' in html


def test_historic_failed_dm_is_preserved_without_unsafe_actions(owner_client, inbox_account):
    message = incoming(inbox_account, "historic-failed")
    reply = InboxReply.objects.create(
        inbox_message=message, status="failed", body="Previous answer", send_error="Timeout"
    )
    response = owner_client.get(detail_url(message), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert "Delivery unverified" in html and "Previous answer" in html
    assert f"replies/{reply.pk}/send/" not in html
    assert f"replies/{reply.pk}/edit/" not in html
    assert f"replies/{reply.pk}/discard/" not in html
    assert response.context["can_save_draft"] is False


def test_confirmed_not_sent_dm_keeps_review_edit_retry_actions(owner_client, inbox_account):
    message = incoming(inbox_account, "known-failed", minutes=-1)
    reply = InboxReply.objects.create(
        inbox_message=message,
        status="failed",
        body="Safe to review",
        send_error="Provider refused",
        not_sent_verified=True,
    )
    response = owner_client.get(detail_url(message), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert "Retry send" in html
    assert f"replies/{reply.pk}/edit/" in html
    assert f"replies/{reply.pk}/discard/" in html


def test_sent_target_disables_new_reply_and_keeps_its_receipt(owner_client, inbox_account):
    message = incoming(inbox_account, "answered")
    reply = InboxReply.objects.create(
        inbox_message=message, status="sent", body="Already delivered", sent_at=timezone.now()
    )
    response = owner_client.get(detail_url(message), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert f'id="reply-{reply.pk}"' in html and "Already delivered" in html
    assert "already has a sent reply" in html
    assert response.context["can_save_draft"] is False
    assert 'disabled aria-describedby="reply-hold-reason"' in html


def test_stale_draft_send_refreshes_confirmed_state_instead_of_leaving_retry(owner_client, inbox_account):
    message = incoming(inbox_account, "comment", kind="comment")
    reply = InboxReply.objects.create(
        inbox_message=message, status="sent", body="Sent by teammate", sent_at=timezone.now()
    )
    url = reverse("inbox:send_reply_draft", kwargs={"workspace_id": message.workspace_id, "reply_id": reply.pk})
    response = owner_client.post(url, HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert response.status_code == 200 and response["HX-Reply-Failed"] == "1"
    assert "Sent by teammate" in html
    assert f"replies/{reply.pk}/send/" not in html


def test_detail_rejects_record_whose_account_moved_workspace(owner_client, inbox_account, organization):
    message = incoming(inbox_account, "moved")
    moved_to = Workspace.objects.create(name="Moved", organization=organization)
    inbox_account.workspace = moved_to
    inbox_account.save(update_fields=["workspace"])
    response = owner_client.get(detail_url(message), HTTP_HX_REQUEST="true")
    assert response.status_code == 404


def test_bulk_action_only_changes_explicit_message_and_preserves_filter(owner_client, inbox_account):
    old = incoming(inbox_account, "old", body="matching old", minutes=-10)
    latest = incoming(inbox_account, "latest", body="matching latest")
    incoming(inbox_account, "unrelated", native="other", body="not in this search")
    url = reverse("inbox:bulk_action", kwargs={"workspace_id": old.workspace_id})
    response = owner_client.post(
        url + "?q=matching", {"message_ids": str(latest.pk), "action": "resolve"}, HTTP_HX_REQUEST="true"
    )
    old.refresh_from_db()
    latest.refresh_from_db()
    assert old.status == "unread" and latest.status == "resolved"
    assert b"not in this search" not in response.content
    assert response.context["inbox_page"].paginator.count == 1


def test_share_and_unavailable_content_render_in_stored_timeline(owner_client, inbox_account):
    first = incoming(
        inbox_account,
        "share",
        body="",
        extra={
            "conversation_id": "thread",
            "inbox_attachments": [
                {"type": "share", "title": "Saved post", "url": "https://www.instagram.com/p/example/"}
            ],
        },
    )
    incoming(inbox_account, "unknown-media", body="", extra={"conversation_id": "thread"})
    response = owner_client.get(detail_url(first), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert "Saved post" in html and "Open original post" in html
    assert "Content unavailable." in html


def test_foreign_selection_id_cannot_retarget_panel(owner_client, inbox_account, organization):
    target = incoming(inbox_account, "target")
    foreign_workspace = Workspace.objects.create(name="Foreign", organization=organization)
    foreign_account = SocialAccount.objects.create(
        workspace=foreign_workspace, platform="facebook", account_platform_id="foreign"
    )
    foreign = incoming(foreign_account, "foreign", body="Never expose this")
    url = reverse("inbox:save_reply_draft", kwargs={"workspace_id": target.workspace_id, "message_id": target.pk})
    response = owner_client.post(url, {"body": "My draft", "panel_message_id": str(foreign.pk)}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert b"Never expose this" not in response.content
    assert f'data-selected-message-id="{target.pk}"' in response.content.decode()
