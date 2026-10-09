"""Fresh session integration of the canonical DB reader, with synthetic records only."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import ConversationMessage, InboxConversation, InboxMessage
from apps.inbox.tests.test_conversation_composer_recovery import composer as _composer

composer = _composer
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def session(client, composer, settings):
    settings.INBOX_CANONICAL_READ_ENABLED = True
    client.force_login(composer.user)
    return client


def route(composer, name="conversation_detail", conversation=None):
    kwargs = {"workspace_id": composer.account.workspace_id}
    if name != "feed":
        kwargs["conversation_id"] = (conversation or composer.conversation).pk
    return reverse(f"inbox:{name}", kwargs=kwargs)


def record(composer, index, *, outbound=False, conversation=None, occurred_at=None, body=None):
    return ConversationMessage.objects.create(
        workspace=composer.account.workspace,
        social_account=composer.account,
        platform=composer.account.platform,
        conversation=conversation or composer.conversation,
        platform_message_id=f"canonical-{index}",
        sender_id=composer.account.account_platform_id if outbound else composer.conversation.peer_id,
        recipient_id=composer.conversation.peer_id if outbound else composer.account.account_platform_id,
        sender_name="Synthetic brand" if outbound else "Synthetic person",
        body=body or f"Canonical message {index}",
        occurred_at=occurred_at or timezone.now() - timedelta(minutes=index + 2),
        direction="outbound" if outbound else "inbound",
        conversation_attribution="platform",
        conversation_type="direct",
        classification_reason="participants_pair",
        delivery_status="observed",
    )


def test_canonical_pages_never_fetch_overlay_and_get_never_creates_transport_rows(session, composer):
    before = InboxMessage.objects.count()
    with patch("apps.inbox.native_thread_reads.read_native_thread") as native:
        for name in ("feed", "conversation_detail"):
            response = session.get(route(composer, name))
            assert response.status_code == 200
            html = response.content.decode()
            assert "inbox-canonical.js" in html
            if name == "conversation_detail":
                assert "inbox-native-thread.js" not in html
            else:
                assert "data-unified-shell" in html and "data-native-thread" not in html
            assert 'hx-history="false"' in html
            assert "no-store" in response["Cache-Control"]
    assert InboxMessage.objects.count() == before
    native.assert_not_called()


def test_search_includes_outbound_without_legacy_body_overlay(session, composer):
    record(
        composer, 1, outbound=True, body="outbound unique needle", occurred_at=timezone.now() - timedelta(seconds=10)
    )
    response = session.get(route(composer, "feed"), {"q": "unique needle"})
    assert [item["id"] for item in response.context["unified_rows"]] == [str(composer.conversation.pk)]
    assert "outbound unique needle" in response.content.decode()


def test_outbound_only_conversation_has_no_invented_incoming(session, composer):
    other = InboxConversation.objects.create(
        workspace=composer.account.workspace,
        social_account=composer.account,
        platform=composer.account.platform,
        platform_conversation_id="outbound-only",
        peer_id=composer.conversation.peer_id,
        identity_kind="platform",
        conversation_type="direct",
    )
    record(composer, 1, outbound=True, conversation=other)
    response = session.get(route(composer, conversation=other))
    assert response.status_code == 200
    assert response.context["canonical_conversation"]["legacy_anchor_id"] is None
    assert "Canonical message 1" in response.content.decode()
    assert not InboxMessage.objects.exists()


def test_paging_returns_only_saved_rows_without_replacing_composer(session, composer):
    for index in range(35):
        record(composer, index)
    response = session.get(route(composer))
    ids = {item["id"] for item in response.context["canonical_messages"]}
    older = session.get(response.context["canonical_older_url"])
    assert older.status_code == 200
    assert not ids & {item["id"] for item in older.context["canonical_messages"]}
    assert "data-canonical-page" in older.content.decode() and "inbox-canonical-composer" not in older.content.decode()


def test_withdrawn_and_expired_content_cannot_appear_in_list_search_or_timeline(session, composer):
    for index, status in enumerate(("removed", "expired")):
        item = record(composer, index, body="HIDDEN PRIVATE CONTENT")
        item.content_status = status
        item.save(update_fields=["content_status"])
    assert "HIDDEN PRIVATE CONTENT" not in session.get(route(composer)).content.decode()
    assert session.get(route(composer, "feed"), {"q": "HIDDEN PRIVATE CONTENT"}).context["unified_rows"] == []


def test_undated_rows_are_separate_and_observed_outbound_does_not_claim_sent(session, composer):
    item = record(composer, 1, outbound=True)
    ConversationMessage.objects.filter(pk=item.pk).update(occurred_at=None)
    response = session.get(route(composer))
    assert [row["id"] for row in response.context["canonical_undated"]] == [str(item.pk)]
    assert "Time unavailable" in response.content.decode() and ">Sent</span>" not in response.content.decode()


def test_unknown_conversation_and_unsupported_filters_fail_closed(session, composer):
    unknown = reverse(
        "inbox:conversation_detail", kwargs={"workspace_id": composer.account.workspace_id, "conversation_id": uuid4()}
    )
    assert session.get(unknown).status_code == 404
    assert session.get(route(composer, "feed"), {"status": "unread"}).status_code == 422
    assert session.get(route(composer, "feed"), {"sentiment": "positive"}).status_code == 400


def test_default_off_route_has_no_canonical_read_access(session, composer, settings):
    settings.INBOX_CANONICAL_READ_ENABLED = False
    assert session.get(route(composer)).status_code == 404
    assert "inbox-native-thread.js" in session.get(route(composer, "feed")).content.decode()


def payload(response, body="Synthetic answer"):
    state = response.context["conversation_composer"]
    return {
        "body": body,
        "composer_action_nonce": state["action_nonce"],
        "composer_revision": state["composer_revision"],
        "composer_scope_token": state["scope_token"],
        "adopt_reply_id": state.get("adopt_reply_id", ""),
    }


def test_draft_save_is_composer_only_and_keeps_one_canonical_intent(session, composer):
    from apps.inbox.models import InboxReply

    initial = session.get(route(composer))
    response = session.post(route(composer, "conversation_save_draft"), payload(initial))
    assert response.status_code == 200 and "HX-Reply-Failed" not in response
    assert response["HX-Retarget"] == "#inbox-canonical-composer"
    assert "data-canonical-page" not in response.content.decode()
    reply = InboxReply.objects.get()
    assert reply.conversation_id == composer.conversation.pk
    assert reply.inbox_message.extra["transport_projection"] is True and reply.inbox_message.body == ""
    edited = session.post(route(composer, "conversation_save_draft"), payload(response, "Edited draft"))
    assert "HX-Reply-Failed" not in edited
    assert InboxReply.objects.count() == 1 and InboxReply.objects.get().body == "Edited draft"


def test_normal_followup_uses_new_nonce_and_duplicate_submission_does_not_send_twice(session, composer):
    from apps.inbox.models import InboxReply
    from apps.inbox.tests.test_conversation_composer_recovery import accepted

    first = payload(session.get(route(composer)), "First distinct reply")
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        sent = session.post(route(composer, "conversation_send_reply"), first)
        duplicate = session.post(route(composer, "conversation_send_reply"), first)
        following = session.post(route(composer, "conversation_send_reply"), payload(sent, "Second distinct reply"))
    assert all("HX-Reply-Failed" not in response for response in (sent, duplicate, following))
    assert provider.call_count == 2
    assert InboxReply.objects.filter(status="sent").count() == 2
    assert (
        "First distinct reply" in following.content.decode() and "Second distinct reply" in following.content.decode()
    )


def test_stale_draft_keeps_authoritative_body_and_separate_recovery_text(session, composer):
    from apps.inbox.models import InboxReply

    initial = session.get(route(composer))
    session.post(route(composer, "conversation_save_draft"), payload(initial, "Authoritative draft"))
    response = session.post(route(composer, "conversation_save_draft"), payload(initial, "Stale unsaved text"))
    assert response["HX-Reply-Failed"] == "1"
    assert response.context["composer_body"] == "Authoritative draft"
    assert response.context["unsaved_draft_body"] == "Stale unsaved text"
    assert InboxReply.objects.get().body == "Authoritative draft"


def test_draft_permission_is_independent_of_send_permission(session, composer):
    from apps.members.models import CustomRole, WorkspaceMembership

    role = CustomRole.objects.create(
        organization=composer.account.workspace.organization,
        name="Read and draft",
        permissions={"use_inbox": True, "reply_from_inbox": False},
    )
    WorkspaceMembership.objects.filter(user=composer.user, workspace=composer.account.workspace).update(
        custom_role=role
    )
    context = session.get(route(composer))
    assert context.context["can_save_draft"] is True and context.context["send_availability"]["allowed"] is False
    saved = session.post(route(composer, "conversation_save_draft"), payload(context))
    assert "HX-Reply-Failed" not in saved
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        assert session.post(route(composer, "conversation_send_reply"), payload(saved)).status_code == 403
    provider.assert_not_called()


def test_opening_old_projection_id_resolves_canonical_and_old_mutations_are_held(session, composer):
    from apps.inbox.models import InboxReply

    saved = session.post(route(composer, "conversation_save_draft"), payload(session.get(route(composer))))
    assert "HX-Reply-Failed" not in saved
    message = InboxReply.objects.get().inbox_message

    def legacy(name):
        return reverse(
            f"inbox:{name}", kwargs={"workspace_id": composer.account.workspace_id, "message_id": message.pk}
        )

    response = session.get(legacy("message_detail"))
    assert 'data-source="canonical"' in response.content.decode()
    assert "Synthetic customer question" in response.content.decode()
    with patch("apps.inbox.native_thread_reads.read_native_thread") as native:
        for name in ("native_thread_refresh", "send_reply", "change_status", "assign", "add_note"):
            assert session.post(legacy(name), {"body": "No legacy send", "status": "resolved"}).status_code == 409
    native.assert_not_called()
    message.refresh_from_db()
    assert message.status == "archived"


def test_signed_rendered_read_ack_does_not_swallow_a_new_arrival(session, composer):
    from apps.inbox.models import ConversationReadState

    composer.conversation.incoming_generation = 1
    composer.conversation.save(update_fields=["incoming_generation"])
    composer.row.incoming_generation = 1
    composer.row.save(update_fields=["incoming_generation"])
    response = session.get(route(composer))
    assert not ConversationReadState.objects.exists()
    token = response.context["read_ack_token"]
    assert token
    composer.conversation.incoming_generation = 2
    composer.conversation.save(update_fields=["incoming_generation"])
    response = session.post(route(composer, "conversation_read_ack"), {"read_ack_token": token})
    assert response.status_code == 200
    assert response.json()["read_state"] == {"read_generation": 1, "incoming_generation": 2, "unread": True}
    assert response.json()["unread_count"] == 1


def test_done_uses_generation_and_revision_without_replacing_editor(session, composer):
    composer.conversation.workflow_baseline_at = timezone.now()
    composer.conversation.incoming_generation = 1
    composer.conversation.save(update_fields=["workflow_baseline_at", "incoming_generation"])
    response = session.get(route(composer))
    state = response.context["canonical_conversation"]
    assert response.context["can_mark_done"] is True
    result = session.post(
        route(composer, "conversation_mark_done"),
        {"incoming_generation": state["incoming_generation"], "conversation_revision": state["revision"]},
    )
    assert result.status_code == 200 and result["HX-Retarget"] == "#inbox-canonical-header"
    assert "inbox-reply-body" not in result.content.decode()
    composer.conversation.refresh_from_db()
    assert composer.conversation.workflow_state == "done"


def test_private_receipt_body_stays_hidden_in_presentation_rollback(session, composer, settings):
    from apps.inbox.models import InboxReply
    from apps.inbox.tests.test_conversation_composer_recovery import accepted

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted):
        session.post(
            route(composer, "conversation_send_reply"), payload(session.get(route(composer)), "PRIVATE RECEIPT")
        )
    reply = InboxReply.objects.get()
    ConversationMessage.objects.filter(legacy_reply=reply).update(content_status="removed", is_deleted=True)
    assert "PRIVATE RECEIPT" not in session.get(route(composer)).content.decode()
    settings.INBOX_CANONICAL_READ_ENABLED = False
    old = reverse(
        "inbox:message_detail",
        kwargs={"workspace_id": composer.account.workspace_id, "message_id": reply.inbox_message_id},
    )
    assert "PRIVATE RECEIPT" not in session.get(old).content.decode()


def test_account_confirmation_describes_the_actual_preserved_history_branch(composer):
    from django.template.loader import render_to_string

    html = render_to_string(
        "social_accounts/partials/_account_card.html",
        {"account": composer.account, "workspace_id": composer.account.workspace_id},
    )
    assert "saved posts, inbox history and delivery receipts remain" in html
    assert "inbox messages and posts made only for it are deleted" not in html


def test_historical_draft_adoption_is_explicit_and_keeps_the_other_draft(session, composer):
    from apps.inbox.canonical_send_target import transport_target
    from apps.inbox.models import InboxReply

    target = transport_target(composer.row, composer.conversation, composer.account, materialize=True)
    first = InboxReply.objects.create(inbox_message=target, body="First historical draft")
    second = InboxReply.objects.create(inbox_message=target, body="Second historical draft")
    initial = session.get(route(composer))
    assert initial.context["composer_body"] == "" and not initial.context["can_save_draft"]
    selected = session.get(route(composer), {"adopt_reply_id": str(second.pk)})
    assert selected.context["composer_body"] == second.body
    second.refresh_from_db()
    assert second.conversation_id is None
    saved = session.post(route(composer, "conversation_save_draft"), payload(selected, second.body))
    assert "HX-Reply-Failed" not in saved
    second.refresh_from_db()
    first.refresh_from_db()
    assert second.conversation_id == composer.conversation.pk and first.conversation_id is None
    assert first.body == "First historical draft" and InboxReply.objects.count() == 2


def test_verified_failure_can_retire_without_a_second_provider_attempt(session, composer):
    from apps.inbox.models import InboxReply

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=NotImplementedError) as provider:
        failed = session.post(route(composer, "conversation_send_reply"), payload(session.get(route(composer))))
    assert provider.call_count == 1 and failed["HX-Reply-Failed"] == "1"
    state = failed.context["conversation_composer"]
    assert state["can_retire_failed"] is True
    reply = InboxReply.objects.get()
    nonce = reply.action_nonce
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = session.post(
            route(composer, "conversation_retire_failed"),
            {
                "reply_id": str(reply.pk),
                "composer_revision": state["composer_revision"],
                "composer_scope_token": state["scope_token"],
            },
        )
    assert response.status_code == 200 and response["HX-Retarget"] == "#inbox-canonical-composer"
    provider.assert_not_called()
    reply.refresh_from_db()
    assert reply.retired_at and reply.action_nonce == nonce and reply.status == "failed"


def test_public_thread_keeps_selected_reply_target_and_mention_facets(session, composer):
    from apps.inbox.tests.test_public_thread_recovery import row

    root = row(composer.account, "root-comment", body="Selected public root", post_id="page_post", parent_id="")
    child = row(
        composer.account,
        "child-comment",
        body="Later public child",
        post_id="page_post",
        parent_id="root-comment",
        is_mention=True,
    )
    url = reverse("inbox:message_detail", kwargs={"workspace_id": composer.account.workspace_id, "message_id": root.pk})
    response = session.get(url)
    assert response.context["public_thread_view"] is True and response.context["reply_target"].pk == root.pk
    html = response.content.decode()
    assert "Selected public root" in html and "Later public child" in html and "data-native-thread" not in html
    for domain in ("comment", "mention"):
        feed = session.get(route(composer, "feed"), {"domain": domain})
        assert feed.status_code == 200
        assert any(item["id"] in {str(root.pk), str(child.pk)} for item in feed.context["unified_rows"])


def test_role_revocation_during_draft_projection_discards_the_response(session, composer):
    from apps.inbox import canonical_views
    from apps.members.models import WorkspaceMembership

    session.post(route(composer, "conversation_save_draft"), payload(session.get(route(composer)), "PRIVATE DRAFT"))
    original = canonical_views._reply_content

    def revoke(reply):
        WorkspaceMembership.objects.filter(user=composer.user, workspace=composer.account.workspace).delete()
        return original(reply)

    with patch.object(canonical_views, "_reply_content", side_effect=revoke):
        response = session.get(route(composer))
    assert response.status_code == 404 and b"PRIVATE DRAFT" not in response.content
