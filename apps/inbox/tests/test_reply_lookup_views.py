"""Read-only lookup candidates never become an automatic delivery decision."""

import sys
from datetime import timedelta
from types import ModuleType
from unittest.mock import Mock, patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.inbox.dm_send_gate import DMSendGateError
from apps.inbox.models import InboxReply, InternalNote
from apps.inbox.tests.test_conversation_presentation import owner_client as owner_client  # noqa: F401
from apps.inbox.tests.test_reply_reconciliation_views import evidence, review_url, uncertain
from apps.members.models import CustomRole, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def lookup(monkeypatch):
    service = Mock(
        return_value={
            "status": "unconfirmed",
            "reason": "No result confirmed",
            "candidates": [],
            "more_available": False,
            "checked_at": timezone.now().isoformat(),
        }
    )
    module = ModuleType("apps.inbox.reply_lookup")
    module.lookup_reply_receipts = service
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return service


def request_data(reply):
    return {
        "action": "lookup",
        "expected_updated_at": reply.updated_at.isoformat(),
        "expected_send_generation": str(reply.send_generation),
    }


def test_lookup_requires_no_outcome_or_confirmation_and_preserves_receipt(owner_client, inbox_account, user, lookup):
    reply = uncertain(inbox_account)
    version = reply.updated_at
    with patch("apps.inbox.reply_reconciliation.reconcile_reply_outcome") as reconcile:
        response = owner_client.post(review_url(reply), request_data(reply))
    reconcile.assert_not_called()
    lookup.assert_called_once()
    args = lookup.call_args.kwargs
    assert args["reply"].pk == reply.pk and args["actor"].pk == user.pk
    assert args["expected_updated_at"] == version
    assert args["expected_send_generation"] == reply.send_generation
    assert response.status_code == 200
    assert response.context["values"]["outcome"] == "" and response.context["values"]["confirmed"] is False
    reply.refresh_from_db()
    assert reply.status == "unknown" and reply.updated_at == version
    assert not InternalNote.objects.exists()
    assert b"missing result is not proof" in response.content


def test_lookup_lists_at_most_five_receipts_and_never_renders_external_bodies(owner_client, inbox_account, lookup):
    reply = uncertain(inbox_account)
    sent_at = (timezone.now() - timedelta(minutes=5)).isoformat()
    lookup.return_value = {
        "status": "candidates",
        "reason": "RAW EXTERNAL RESPONSE",
        "body": "Unrelated external body",
        "candidates": [
            {
                "platform_reply_id": f"possible-id-{index}",
                "sent_at": sent_at,
                "body": "Private external message",
                "sender_id": "private-sender",
            }
            for index in range(7)
        ],
        "more_available": True,
        "checked_at": timezone.now().isoformat(),
    }
    response = owner_client.post(review_url(reply), request_data(reply))
    html = response.content.decode()
    assert response.status_code == 200
    assert len(response.context["lookup_result"]["candidates"]) == 5
    assert "possible-id-4" in html and "possible-id-5" not in html
    assert "Private external message" not in html and "private-sender" not in html
    assert "RAW EXTERNAL RESPONSE" not in html and "Unrelated external body" not in html
    assert "No additional page was loaded" in html
    assert response.context["values"]["outcome"] == ""
    assert response.context["values"]["confirmed"] is False
    assert response.context["values"]["platform_reply_id"] == ""


def test_lookup_action_ignores_any_injected_manual_decision_fields(owner_client, inbox_account, lookup):
    reply = uncertain(inbox_account)
    with patch("apps.inbox.reply_reconciliation.reconcile_reply_outcome") as reconcile:
        response = owner_client.post(review_url(reply), {**evidence(reply, "sent"), **request_data(reply)})
    reconcile.assert_not_called()
    assert response.context["values"]["outcome"] == ""
    assert response.context["values"]["confirmed"] is False
    reply.refresh_from_db()
    assert reply.status == "unknown"


def test_unexpected_lookup_failure_is_generic_and_cannot_change_outcome(owner_client, inbox_account, lookup):
    reply = uncertain(inbox_account)
    lookup.side_effect = RuntimeError("Private provider message and raw response token")
    response = owner_client.post(review_url(reply), request_data(reply))
    assert response.status_code == 200
    assert b"could not confirm whether the reply was sent" in response.content
    assert b"Private provider message" not in response.content
    reply.refresh_from_db()
    assert reply.status == "unknown" and not reply.not_sent_verified


def test_stale_lookup_is_409_and_preserves_original_review_version(owner_client, inbox_account, lookup):
    reply = uncertain(inbox_account)
    lookup.side_effect = DMSendGateError("reconciliation_stale", "This reply changed. Reload before reviewing.")
    data = request_data(reply)
    response = owner_client.post(review_url(reply), data)
    assert response.status_code == 409
    assert response.context["values"]["expected_updated_at"] == data["expected_updated_at"]
    assert response.context["values"]["outcome"] == ""
    assert not InternalNote.objects.exists()


def test_invalid_review_version_stops_before_lookup(owner_client, inbox_account, lookup):
    reply = uncertain(inbox_account)
    response = owner_client.post(review_url(reply), {"action": "lookup", "expected_updated_at": "invalid"})
    assert response.status_code == 400
    lookup.assert_not_called()


@pytest.mark.parametrize("missing", ["use_inbox", "manage_workspace_settings", "reply_from_inbox"])
def test_lookup_uses_both_existing_permissions(owner_client, inbox_account, user, organization, lookup, missing):
    reply = uncertain(inbox_account)
    role = CustomRole.objects.create(
        organization=organization,
        name=f"Lookup missing {missing}",
        permissions={"use_inbox": True, "manage_workspace_settings": True, "reply_from_inbox": True, missing: False},
    )
    WorkspaceMembership.objects.filter(user=user, workspace=inbox_account.workspace).update(custom_role=role)
    assert owner_client.post(review_url(reply), request_data(reply)).status_code == 403
    lookup.assert_not_called()


def test_lookup_post_requires_csrf(owner_client, inbox_account, user, lookup):
    reply = uncertain(inbox_account)
    secure = Client(enforce_csrf_checks=True)
    secure.force_login(user)
    assert secure.post(review_url(reply), request_data(reply)).status_code == 403
    lookup.assert_not_called()


def test_foreign_workspace_cannot_trigger_a_lookup(owner_client, inbox_account, organization, lookup):
    foreign_workspace = Workspace.objects.create(name="Foreign", organization=organization)
    account = SocialAccount.objects.create(
        workspace=foreign_workspace, platform="facebook", account_platform_id="foreign"
    )
    reply = uncertain(account)
    response = owner_client.post(review_url(reply, workspace=inbox_account.workspace), request_data(reply))
    assert response.status_code == 404
    lookup.assert_not_called()


@pytest.mark.parametrize("generation", [None, "-1", "invalid", "1.5"])
def test_lookup_requires_valid_send_generation(owner_client, inbox_account, lookup, generation):
    reply = uncertain(inbox_account)
    data = request_data(reply)
    if generation is None:
        data.pop("expected_send_generation")
    else:
        data["expected_send_generation"] = generation
    response = owner_client.post(review_url(reply), data)
    assert response.status_code == 400
    lookup.assert_not_called()


def test_lookup_rejects_new_generation_without_replacing_the_reviewed_version(owner_client, inbox_account, lookup):
    reply = uncertain(inbox_account)
    data = request_data(reply)
    InboxReply.objects.filter(pk=reply.pk).update(send_generation=reply.send_generation + 1)
    response = owner_client.post(review_url(reply), data)
    assert response.status_code == 409
    assert response.context["values"]["expected_send_generation"] == data["expected_send_generation"]
    assert response.context["values"]["expected_updated_at"] == data["expected_updated_at"]
    lookup.assert_not_called()
