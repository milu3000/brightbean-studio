"""Session observation tokens must describe the history actually displayed."""

from datetime import timedelta
from unittest.mock import patch

import pytest

from apps.inbox.models import InboxReply
from apps.inbox.tests.test_canonical_session_recovery import payload, route
from apps.inbox.tests.test_owned_composer_bridge import accepted, incoming
from apps.inbox.tests.test_owned_composer_bridge import clock as _clock
from apps.inbox.tests.test_owned_composer_bridge import owner as _owner
from apps.inbox.tests.test_pending_history_recovery import historical

owner, clock = _owner, _clock
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def session(client, owner):
    client.force_login(owner.user)
    return client


def posted(response, text):
    values = payload(response, text)
    values["composer_observation_token"] = response.context["composer_observation_token"]
    return values


def test_new_activity_preserves_text_and_only_latest_can_restore_current_owner_send(session, owner):
    initial = session.get(route(owner))
    assert initial.context["send_availability"]["allowed"]
    values = posted(initial, "Keep this unsent answer")
    original_token, original_scope = values["composer_observation_token"], values["composer_scope_token"]
    incoming(owner)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        stale = session.post(route(owner, "conversation_send_reply"), values)
        provider.assert_not_called()
        assert stale.status_code == 200 and "HX-Reply-Failed" in stale
        assert stale.context["composer_observation_token"] == original_token
        assert stale.context["conversation_composer"]["scope_token"] == original_scope
        assert stale.context["composer_body"] == "Keep this unsent answer"
        assert "data-inbox-needs-latest" in stale.content.decode()
        assert not stale.context["send_availability"]["allowed"]
        newest = session.get(route(owner), {"fragment": "history"})
        assert newest.context["composer_observation_token"] != original_token
        retry = posted(stale, "Keep this unsent answer")
        assert str(newest.context["conversation_composer"]["composer_revision"]) == str(retry["composer_revision"])
        retry["composer_observation_token"] = newest.context["composer_observation_token"]
        retry["composer_scope_token"] = newest.context["conversation_composer"]["scope_token"]
        sent = session.post(route(owner, "conversation_send_reply"), retry)
        assert "HX-Reply-Failed" not in sent
        assert provider.call_count == 1
    assert InboxReply.objects.filter(status="sent", body="Keep this unsent answer").count() == 1


def test_old_drafts_do_not_block_sequential_owned_conversation_messages(session, owner):
    drafts = historical(owner, 24)
    chosen = session.get(route(owner), {"adopt_reply_id": str(drafts[-1].pk)})
    assert chosen.context["composer_body"] == drafts[-1].body
    assert len(chosen.context["conversation_composer"]["legacy_drafts"]) <= 10
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        first = session.post(route(owner, "conversation_send_reply"), posted(chosen, drafts[-1].body))
        assert "HX-Reply-Failed" not in first
        assert not first.context["conversation_composer"]["requires_legacy_adoption"]
        assert first.context["send_availability"]["allowed"]
        second = session.post(route(owner, "conversation_send_reply"), posted(first, "Separate follow-up"))
        assert "HX-Reply-Failed" not in second and provider.call_count == 2
    assert InboxReply.objects.filter(status="sent").count() == 2
    assert InboxReply.objects.filter(conversation__isnull=True, status="draft").count() == 23


def test_manual_standard_reply_and_draft_stay_available_after_24_hours(session, owner):
    owner.clock.now += timedelta(hours=25)
    response = session.get(route(owner))
    assert response.status_code == 200
    assert response.context["send_availability"]["allowed"]
    assert response.context["can_save_draft"]
    assert "human_agent_permission_unverified" not in response.content.decode()
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        saved = session.post(route(owner, "conversation_save_draft"), posted(response, "Keep a draft for later"))
    assert "HX-Reply-Failed" not in saved
    assert saved.context["composer_body"] == "Keep a draft for later"
    provider.assert_not_called()
