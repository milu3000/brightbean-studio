"""Fresh UI quote/retirement proofs over the verified native quote service."""

from unittest.mock import patch
from uuid import uuid4

import pytest

from apps.inbox.models import ConversationMessage, InboxReply
from apps.inbox.tests.test_canonical_session_recovery import payload, record, route
from apps.inbox.tests.test_canonical_session_recovery import session as _session
from apps.inbox.tests.test_conversation_composer_recovery import accepted
from apps.inbox.tests.test_conversation_composer_recovery import composer as _composer

composer = _composer
session = _session
pytestmark = pytest.mark.django_db(transaction=True)


def quoted(response, target="", body="Quote reply"):
    return {**payload(response, body), "quote_target_id": str(target)}


def test_default_has_no_quote_and_visible_incoming_and_outgoing_are_explicit_targets(session, composer):
    outgoing = record(composer, 1, outbound=True)
    response = session.get(route(composer))
    state = response.context["conversation_composer"]
    assert state["quote_supported"] and not state["quote_target_id"]
    html = response.content.decode()
    assert 'data-inbox-quote-initial="" value=""' in html
    for target in (composer.row, outgoing):
        assert f'data-inbox-quote-target="{target.pk}"' in html
    assert html.index("inbox-quote.js") < html.index("inbox-canonical.js")


def test_select_save_reopen_and_cancel_keep_one_draft(session, composer):
    initial = session.get(route(composer))
    saved = session.post(route(composer, "conversation_save_draft"), quoted(initial, composer.row.pk))
    assert "HX-Reply-Failed" not in saved
    reply = InboxReply.objects.get()
    assert reply.quote_target_id == composer.row.pk
    reopened = session.get(route(composer))
    assert reopened.context["conversation_composer"]["quote_preview"]["body"] == composer.row.body
    cleared = session.post(route(composer, "conversation_save_draft"), quoted(reopened))
    assert "HX-Reply-Failed" not in cleared
    reply.refresh_from_db()
    assert reply.quote_target_id is None and reply.body == "Quote reply"
    assert InboxReply.objects.count() == 1


def test_outbound_quote_is_explicit_and_confirmed_send_starts_next_action_without_quote(session, composer):
    outgoing = record(composer, 1, outbound=True)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        response = session.post(
            route(composer, "conversation_send_reply"), quoted(session.get(route(composer)), outgoing.pk)
        )
    assert "HX-Reply-Failed" not in response
    assert InboxReply.objects.get(status="sent").quote_target_id == outgoing.pk
    assert not response.context["conversation_composer"]["quote_target_id"]
    provider.assert_called_once()


def test_stale_quote_only_cancel_cannot_clear_the_saved_quote(session, composer):
    initial = session.get(route(composer))
    session.post(route(composer, "conversation_save_draft"), quoted(initial, composer.row.pk))
    response = session.post(route(composer, "conversation_save_draft"), quoted(initial))
    assert response["HX-Reply-Failed"] == "1" and response.context["unsaved_quote_selection"]
    assert InboxReply.objects.get().quote_target_id == composer.row.pk


@pytest.mark.parametrize("status", ["removed", "expired", "unavailable"])
def test_restricted_quote_preview_has_no_body_or_target_button_and_can_be_canceled(session, composer, status):
    saved = session.post(
        route(composer, "conversation_save_draft"), quoted(session.get(route(composer)), composer.row.pk)
    )
    assert "HX-Reply-Failed" not in saved
    ConversationMessage.objects.filter(pk=composer.row.pk).update(
        content_status=status, is_deleted=status == "removed", body="PRIVATE WITHHELD TEXT"
    )
    response = session.get(route(composer))
    state = response.context["conversation_composer"]
    assert state["quote_preview"]["unavailable"] and not state["quote_preview"]["body"]
    assert response.context["can_save_draft"] is True and response.context["send_availability"]["allowed"] is False
    assert "PRIVATE WITHHELD TEXT" not in response.content.decode()
    assert f'data-inbox-quote-target="{composer.row.pk}"' not in response.content.decode()
    cleared = session.post(route(composer, "conversation_save_draft"), quoted(response))
    assert "HX-Reply-Failed" not in cleared and InboxReply.objects.get().quote_target_id is None


def test_unknown_or_foreign_quote_is_rejected_without_provider_call(session, composer):
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = session.post(
            route(composer, "conversation_send_reply"), quoted(session.get(route(composer)), uuid4())
        )
    assert response["HX-Reply-Failed"] == "1" and response.context["unsaved_quote_selection"]
    provider.assert_not_called()
    assert not InboxReply.objects.exists()


def test_retiring_unattempted_draft_keeps_body_and_nonce_and_leaves_blank_composer(session, composer):
    saved = session.post(
        route(composer, "conversation_save_draft"), quoted(session.get(route(composer)), composer.row.pk)
    )
    state = saved.context["conversation_composer"]
    assert state["can_retire_draft"]
    original = InboxReply.objects.get()
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = session.post(
            route(composer, "conversation_retire_draft"),
            {
                "reply_id": original.pk,
                "composer_revision": state["composer_revision"],
                "composer_scope_token": state["scope_token"],
            },
        )
    provider.assert_not_called()
    assert response.status_code == 200 and response.context["composer_body"] == ""
    retained = InboxReply.objects.get()
    assert retained.retired_at and retained.action_nonce == original.action_nonce and retained.body == original.body
    assert "Saved previous draft" in response.content.decode()
