"""Opaque references keep the existing context and action identity intact."""

import json
from unittest.mock import patch

import pytest
from django.core import signing

from apps.inbox.inbox_intents import SALT
from apps.inbox.models import InboxReply
from apps.inbox.tests.test_canonical_reads_rebuilt import proof
from apps.inbox.tests.test_composer_surfaces_recovery import composer as composer
from apps.inbox.tests.test_composer_surfaces_recovery import surface as surface
from apps.mcp.saved_inbox_tools import _read_intent
from apps.members.models import WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def intent_context(composer, surface, settings):
    settings.INBOX_CANONICAL_READ_ENABLED = True
    proof(composer, composer.row)
    return composer, *surface


def read(client, composer):
    response = client.get(f"/api/v1/inbox/saved/targets/{composer.conversation.pk}", secure=True)
    assert response.status_code == 200, response.content
    return response.json()


def post(client, action, payload):
    return client.post(
        f"/api/v1/inbox/saved/{action}", json.dumps(payload), content_type="application/json", secure=True
    )


def accepted(*args, **kwargs):
    kwargs["before_provider"]()
    return "synthetic-intent-accepted"


def test_read_facade_returns_actual_context_and_catalog_action_reference(intent_context):
    composer, client, key = intent_context
    before = InboxReply.objects.count()
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        value = read(client, composer)
    assert value["reference"]
    assert value["conversation"]["conversation"]["id"] == str(composer.conversation.pk)
    assert "read_ack_token" not in value["conversation"]
    assert InboxReply.objects.count() == before
    provider.assert_not_called()
    member = WorkspaceMembership.objects.get(user=composer.user, workspace=composer.account.workspace)
    mcp = _read_intent({"target_id": str(composer.conversation.pk)}, {"api_key": key, "membership": member})
    payload = json.loads(mcp["content"][0]["text"])
    assert payload["conversation"]["messages"] == value["conversation"]["messages"]


def test_same_reference_sends_one_action_and_status_is_acceptance(intent_context):
    composer, client, _key = intent_context
    value = read(client, composer)
    payload = {"reference": value["reference"], "body": "Synthetic facade answer"}
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        first = post(client, "send", payload)
        again = post(client, "send", payload)
    assert first.status_code == again.status_code == 200, (first.content, again.content)
    assert first.json()["receipt"]["id"] == again.json()["receipt"]["id"]
    assert provider.call_count == 1
    status = post(client, "status", {"reference": value["reference"]})
    assert status.status_code == 200
    assert status.json()["transport_state"] == "provider_accepted"


def test_save_keeps_original_observation_and_cannot_resend_changed_body(intent_context):
    composer, client, _key = intent_context
    value = read(client, composer)
    original = signing.loads(value["reference"], salt=SALT)["payload"]["observation_token"]
    saved = post(client, "draft", {"reference": value["reference"], "body": "Synthetic saved facade answer"})
    assert saved.status_code == 200, saved.content
    after = signing.loads(saved.json()["reference"], salt=SALT)["payload"]["observation_token"]
    assert original == after, "Saving must not renew unseen history proof"
    payload = {"reference": saved.json()["reference"], "body": "Synthetic saved facade answer"}
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider:
        sent = post(client, "send", payload)
        changed = post(client, "send", {**payload, "body": "Different text for same action"})
    assert sent.status_code == 200, sent.content
    assert changed.status_code == 409
    assert provider.call_count == 1


def test_unknown_outcome_keeps_same_reference_held(intent_context):
    composer, client, _key = intent_context
    value = read(client, composer)
    payload = {"reference": value["reference"], "body": "Synthetic uncertain answer"}

    def unknown(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError("synthetic timeout")

    with patch("apps.inbox.services._dispatch_to_platform", side_effect=unknown) as provider:
        first = post(client, "send", payload)
        again = post(client, "send", payload)
    assert first.status_code == again.status_code == 409
    assert provider.call_count == 1
    status = post(client, "status", {"reference": value["reference"]})
    assert status.json()["transport_state"] == "unknown"


def test_stale_reference_cannot_silently_refresh_and_send(intent_context):
    from django.db.models import F

    composer, client, _key = intent_context
    value = read(client, composer)
    type(composer.conversation).objects.filter(pk=composer.conversation.pk).update(revision=F("revision") + 1)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        result = post(client, "send", {"reference": value["reference"], "body": "Stale synthetic answer"})
    assert result.status_code == 409
    provider.assert_not_called()


def test_reference_is_bound_to_current_key_not_just_same_user(intent_context):
    from django.test import Client

    from apps.api_keys.services import issue_api_key

    composer, client, _key = intent_context
    value = read(client, composer)
    issued = issue_api_key(
        workspace=composer.account.workspace,
        social_accounts=[composer.account],
        issued_by=composer.user,
        name="Synthetic second caller",
        permissions=["use_inbox", "reply_from_inbox"],
    )
    other = Client(HTTP_AUTHORIZATION="Bearer " + issued.plaintext_token)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        result = post(other, "send", {"reference": value["reference"], "body": "Other synthetic caller"})
    assert result.status_code == 409
    provider.assert_not_called()


def test_modified_reference_cannot_send(intent_context):
    composer, client, _key = intent_context
    value = read(client, composer)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        result = post(
            client, "send", {"reference": value["reference"] + "tampered", "body": "Synthetic tampered action"}
        )
    assert result.status_code == 409
    provider.assert_not_called()


def test_expired_write_reference_still_reads_receipt_but_cannot_send(intent_context):
    import time

    composer, client, _key = intent_context
    value = read(client, composer)
    data = signing.loads(value["reference"], salt=SALT)
    with patch("django.core.signing.time.time", return_value=time.time() - 7200):
        old = signing.dumps(data, salt=SALT, compress=True)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        status = post(client, "status", {"reference": old})
        send = post(client, "send", {"reference": old, "body": "Expired synthetic action"})
    assert status.status_code == 200 and status.json()["transport_state"] == "not_attempted"
    assert send.status_code == 409
    provider.assert_not_called()
