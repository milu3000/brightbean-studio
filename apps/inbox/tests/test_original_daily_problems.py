"""Reproduce the user's lag, missing native outgoing and shared FB/IG UI."""

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.template.loader import render_to_string
from django.urls import reverse

from apps.inbox import canonical_reads as reader
from apps.inbox.durable_sync import claim_page, commit_page, start_scan
from apps.inbox.models import ConversationMessage, InboxMessage
from apps.inbox.sync_contracts import SyncPage
from apps.inbox.tests.test_canonical_reads_rebuilt import proof
from apps.inbox.tests.test_canonical_session_recovery import composer as composer
from apps.inbox.tests.test_canonical_session_recovery import payload, route
from apps.inbox.tests.test_canonical_session_recovery import session as session
from apps.inbox.tests.test_conversation_composer_recovery import accepted
from apps.inbox.tests.test_durable_pages_recovery import durable as durable
from apps.inbox.tests.test_durable_pages_recovery import message
from apps.members.models import WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def test_own_app_send_is_in_main_history_in_same_response_before_any_echo(session, composer):
    proof(composer, composer.row)
    before = session.get(route(composer))
    body = "Synthetic immediate own app response"
    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider,
        patch("apps.inbox.native_thread_reads.read_native_thread") as transient,
    ):
        sent = session.post(route(composer, "conversation_send_reply"), payload(before, body))
    assert sent.status_code == 200 and "HX-Reply-Failed" not in sent
    assert body.encode() in sent.content
    saved = ConversationMessage.objects.get(body=body, direction="outbound")
    assert saved.delivery_status == "provider_accepted" and "app_send" in saved.sources
    assert provider.call_count == 1
    transient.assert_not_called()
    reopened = session.get(route(composer))
    assert body.encode() in reopened.content


def test_native_platform_outgoing_enters_database_and_original_ui_without_transient_read(
    durable, user, org_owner, client, settings, enroll_conversation_accounts
):
    settings.INBOX_CANONICAL_READ_ENABLED = True
    enroll_conversation_accounts(durable.social_account, read=True)
    WorkspaceMembership.objects.create(user=user, workspace=durable.workspace, workspace_role="owner")
    lease = claim_page(start_scan(durable.pk, context="backfill", stream="messages", scope_key="thread-1").pk)
    observed = replace(
        message(lease, mid="native-manual-outgoing", body="Synthetic platform-side direct send"),
        sender_id=durable.account_platform_id,
        recipient_id="peer-1",
    )
    commit_page(lease, SyncPage((observed,)))
    saved = ConversationMessage.objects.get(platform_message_id=observed.platform_message_id)
    assert saved.direction == "outbound" and saved.body == observed.body
    assert not InboxMessage.objects.filter(platform_message_id=observed.platform_message_id).exists()
    client.force_login(user)
    url = reverse(
        "inbox:conversation_detail",
        kwargs={"workspace_id": durable.workspace_id, "conversation_id": saved.conversation_id},
    )
    with patch("apps.inbox.native_thread_reads.read_native_thread") as native:
        response = client.get(url, secure=True)
        api_data = reader.read_conversation(
            reader.session_read_scope(user, durable.workspace_id), saved.conversation_id
        )
    assert response.status_code == 200, response.content
    assert observed.body.encode() in response.content
    assert [row["id"] for row in api_data["messages"]] == [str(saved.pk)]
    assert api_data["source"] == "canonical" and api_data["persisted"] is True
    native.assert_not_called()


@pytest.mark.parametrize("platform", ["facebook", "instagram_login"])
def test_fb_and_ig_use_same_accepted_status_not_bare_sent(platform):
    html = render_to_string(
        "inbox/partials/_canonical_message.html",
        {
            "item": {
                "id": str(uuid4()),
                "direction": "outbound",
                "delivery_status": "provider_accepted",
                "body": "Synthetic accepted body",
            },
            "canonical_conversation": {"id": uuid4(), "platform": platform, "account_name": "Synthetic brand"},
            "workspace": {"id": uuid4()},
        },
    )
    assert ">Accepted</span>" in html
    assert ">Delivered</span>" not in html and ">Read</span>" not in html
