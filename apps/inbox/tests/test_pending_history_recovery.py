"""Pending historical text remains reachable without growing one response forever."""

from unittest.mock import patch

import pytest

from apps.inbox.canonical_send_target import transport_target
from apps.inbox.models import InboxReply
from apps.inbox.tests.test_canonical_session_recovery import composer as _composer
from apps.inbox.tests.test_canonical_session_recovery import payload, route
from apps.inbox.tests.test_canonical_session_recovery import session as _session

composer, session = _composer, _session
pytestmark = pytest.mark.django_db(transaction=True)


def historical(composer, count):
    composer.row.refresh_from_db()
    target = transport_target(composer.row, composer.conversation, composer.account, materialize=True)
    return [
        InboxReply.objects.create(inbox_message=target, body=f"Preserved historical {index}") for index in range(count)
    ]


def test_pending_pages_are_bounded_reachable_and_do_not_replace_the_composer(session, composer):
    drafts = historical(composer, 27)
    response = session.get(route(composer))
    seen = []
    while True:
        values = response.context["pending_replies"]
        assert len(values) <= 10
        seen.extend(str(value.pk) for value in values)
        next_url = response.context["pending_next_url"]
        if not next_url:
            break
        response = session.get(next_url)
        assert response.status_code == 200
        assert "inbox-pending-history" in response.content.decode()
        assert "data-inbox-reply-form" not in response.content.decode()
    assert set(seen) == {str(value.pk) for value in drafts} and len(seen) == 27
    assert InboxReply.objects.count() == 27


def test_direct_adoption_remains_available_for_a_draft_outside_history_first_page(session, composer):
    drafts = historical(composer, 24)
    initial = session.get(route(composer))
    assert drafts[0].pk not in {row.pk for row in initial.context["pending_replies"]}
    selected = session.get(route(composer), {"adopt_reply_id": str(drafts[0].pk)})
    assert selected.context["composer_body"] == drafts[0].body
    subsequent = session.get(selected.context["pending_next_url"])
    assert subsequent.status_code == 200
    assert drafts[0].pk not in {row.pk for row in subsequent.context["pending_replies"]}
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        saved = session.post(route(composer, "conversation_save_draft"), payload(selected, drafts[0].body))
    assert "HX-Reply-Failed" not in saved
    provider.assert_not_called()
    assert InboxReply.objects.count() == 24


def test_active_unknown_receipt_stays_pinned_while_historical_pages_change(session, composer):
    response = session.post(
        route(composer, "conversation_save_draft"), payload(session.get(route(composer)), "Unknown receipt body")
    )
    assert "HX-Reply-Failed" not in response
    active = InboxReply.objects.get()
    InboxReply.objects.filter(pk=active.pk).update(status="unknown")
    historical(composer, 15)
    initial = session.get(route(composer))
    assert initial.context["pinned_receipt"].pk == active.pk
    assert active.pk not in {reply.pk for reply in initial.context["pending_replies"]}
    assert "Unknown receipt body" in initial.content.decode()
    page = session.get(initial.context["pending_next_url"])
    assert "Unknown receipt body" not in page.content.decode()
    assert "data-inbox-reply-form" not in page.content.decode()


def test_pending_cursor_does_not_survive_scope_revocation(session, composer, settings):
    historical(composer, 12)
    older = session.get(route(composer)).context["pending_next_url"]
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    assert session.get(older).status_code in {404, 409}
