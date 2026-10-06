"""Manager review records explicit findings without sending or guessing."""

import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import DMSendControl, InboxReply, InternalNote
from apps.inbox.tests.test_conversation_presentation import detail_url, incoming
from apps.inbox.tests.test_conversation_presentation import owner_client as owner_client  # noqa: F401
from apps.members.models import CustomRole, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def uncertain(account, *, status="unknown", body="Reply awaiting delivery review"):
    message = incoming(account, f"review-{InboxReply.objects.count()}", minutes=-15)
    return InboxReply.objects.create(
        inbox_message=message, status=status, body=body, send_error="Previous delivery was not verified"
    )


def review_url(reply, *, workspace=None):
    return reverse(
        "inbox:review_reply_outcome",
        kwargs={"workspace_id": (workspace or reply.inbox_message.workspace).pk, "reply_id": reply.pk},
    )


def evidence(reply, outcome="not_sent"):
    return {
        "expected_updated_at": reply.updated_at.isoformat(),
        "expected_send_generation": str(reply.send_generation),
        "outcome": outcome,
        "confirmed": "yes",
        **(
            {
                "platform_reply_id": "verified-provider-mid",
                "sent_at": timezone.now().isoformat(),
            }
            if outcome == "sent"
            else {}
        ),
    }


def test_get_has_no_default_outcome_or_confirmation_and_never_changes_receipt(owner_client, inbox_account):
    reply = uncertain(inbox_account)
    version = reply.updated_at
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = owner_client.get(review_url(reply))
    provider.assert_not_called()
    reply.refresh_from_db()
    html = response.content.decode()
    assert response.status_code == 200
    assert response.context["values"]["outcome"] == ""
    assert response.context["values"]["confirmed"] is False
    radios = re.findall(r'<input[^>]*name="outcome"[^>]*>', html)
    confirmation = re.search(r'<input[^>]*name="confirmed"[^>]*>', html).group(0)
    assert len(radios) == 2
    assert not any(re.search(r"\schecked(?:\s|=|>)", field) for field in [*radios, confirmation])
    assert "not proof that it was not sent" in html
    assert "manual finding" in html and "Record verified result" in html
    assert reply.status == "unknown" and reply.updated_at == version
    assert InternalNote.objects.count() == 0


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
@pytest.mark.parametrize("prior_status", ["unknown", "failed"])
def test_explicit_review_records_either_outcome_without_dispatch(
    owner_client, inbox_account, user, outcome, prior_status
):
    reply = uncertain(inbox_account, status=prior_status)
    body = reply.body
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        response = owner_client.post(review_url(reply), evidence(reply, outcome))
    provider.assert_not_called()
    reply.refresh_from_db()
    assert response.status_code == 302 and response.url == detail_url(reply.inbox_message)
    assert reply.status == ("sent" if outcome == "sent" else "failed")
    assert reply.not_sent_verified is (outcome == "not_sent")
    assert reply.body == body
    note = InternalNote.objects.get(inbox_message=reply.inbox_message)
    assert note.author == user and "review" in note.body.lower()
    assert InboxReply.objects.count() == 1


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_affirmative_confirmation_is_required_for_both_outcomes(owner_client, inbox_account, outcome):
    reply = uncertain(inbox_account)
    data = evidence(reply, outcome)
    data.pop("confirmed")
    with patch("apps.inbox.reply_reconciliation.reconcile_reply_outcome") as reconcile:
        response = owner_client.post(review_url(reply), data)
    reconcile.assert_not_called()
    reply.refresh_from_db()
    assert response.status_code == 400
    assert reply.status == "unknown" and not reply.not_sent_verified
    assert "Confirm that you personally checked" in response.content.decode()


@pytest.mark.parametrize(
    "invalid",
    [
        {"outcome": ""},
        {"outcome": "unknown"},
        {"platform_reply_id": ""},
        {"sent_at": "2026-10-06T12:30:00"},
        {"sent_at": "not a date"},
        {"expected_updated_at": ""},
    ],
)
def test_invalid_evidence_cannot_change_the_receipt(owner_client, inbox_account, invalid):
    reply = uncertain(inbox_account)
    response = owner_client.post(review_url(reply), {**evidence(reply, "sent"), **invalid})
    reply.refresh_from_db()
    assert response.status_code == 400
    assert reply.status == "unknown"
    assert not InternalNote.objects.exists()


def test_stale_review_is_409_and_preserves_entered_values_and_original_version(owner_client, inbox_account):
    reply = uncertain(inbox_account)
    data = evidence(reply, "sent")
    InboxReply.objects.filter(pk=reply.pk).update(updated_at=reply.updated_at + timedelta(seconds=1))
    response = owner_client.post(review_url(reply), data)
    reply.refresh_from_db()
    html = response.content.decode()
    assert response.status_code == 409 and reply.status == "unknown"
    assert response.context["values"]["expected_updated_at"] == data["expected_updated_at"]
    assert response.context["values"]["platform_reply_id"] == data["platform_reply_id"]
    assert response.context["values"]["sent_at"] == data["sent_at"]
    assert data["platform_reply_id"] in html and data["sent_at"] in html
    assert "Reload" in html and not InternalNote.objects.exists()


@pytest.mark.parametrize("missing", ["use_inbox", "manage_workspace_settings", "reply_from_inbox"])
def test_both_existing_permissions_are_required_for_page_and_submission(
    owner_client, inbox_account, user, organization, missing
):
    reply = uncertain(inbox_account)
    role = CustomRole.objects.create(
        organization=organization,
        name=f"Missing {missing}",
        permissions={"use_inbox": True, "manage_workspace_settings": True, "reply_from_inbox": True, missing: False},
    )
    WorkspaceMembership.objects.filter(user=user, workspace=inbox_account.workspace).update(custom_role=role)
    assert owner_client.get(review_url(reply)).status_code == 403
    assert owner_client.post(review_url(reply), evidence(reply)).status_code == 403
    panel = owner_client.get(detail_url(reply.inbox_message), HTTP_HX_REQUEST="true")
    if missing == "use_inbox":
        assert panel.status_code == 403
    else:
        assert b"Ask a workspace administrator" in panel.content
    assert b"Review delivery outcome</a>" not in panel.content
    reply.refresh_from_db()
    assert reply.status == "unknown"


@pytest.mark.parametrize("generation", [None, "-1", "not-an-integer", "1.5"])
def test_manual_review_requires_valid_send_generation(owner_client, inbox_account, generation):
    reply = uncertain(inbox_account)
    data = evidence(reply)
    if generation is None:
        data.pop("expected_send_generation")
    else:
        data["expected_send_generation"] = generation
    with patch("apps.inbox.reply_reconciliation.reconcile_reply_outcome") as reconcile:
        response = owner_client.post(review_url(reply), data)
    reconcile.assert_not_called()
    assert response.status_code == 400


def test_manual_review_rejects_new_send_generation_even_with_identical_timestamp(owner_client, inbox_account):
    reply = uncertain(inbox_account)
    data = evidence(reply)
    InboxReply.objects.filter(pk=reply.pk).update(send_generation=reply.send_generation + 1)
    with patch("apps.inbox.reply_reconciliation.reconcile_reply_outcome") as reconcile:
        response = owner_client.post(review_url(reply), data)
    reconcile.assert_not_called()
    assert response.status_code == 409
    assert response.context["values"]["expected_send_generation"] == data["expected_send_generation"]
    assert response.context["values"]["expected_updated_at"] == data["expected_updated_at"]


def test_foreign_workspace_reply_is_not_exposed(owner_client, inbox_account, organization):
    foreign_workspace = Workspace.objects.create(name="Private", organization=organization)
    foreign_account = SocialAccount.objects.create(
        workspace=foreign_workspace, platform="facebook", account_platform_id="foreign"
    )
    reply = uncertain(foreign_account, body="Private foreign reply")
    target = review_url(reply, workspace=inbox_account.workspace)
    for response in (owner_client.get(target), owner_client.post(target, evidence(reply))):
        assert response.status_code == 404
        assert b"Private foreign reply" not in response.content


def test_csrf_is_required_even_for_an_authorized_manager(owner_client, inbox_account, user):
    reply = uncertain(inbox_account)
    secure = Client(enforce_csrf_checks=True)
    secure.force_login(user)
    response = secure.post(review_url(reply), evidence(reply))
    assert response.status_code == 403
    page = secure.get(review_url(reply))
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
    response = secure.post(review_url(reply), {**evidence(reply), "csrfmiddlewaretoken": token})
    assert response.status_code == 302
    reply.refresh_from_db()
    assert reply.not_sent_verified is True


def test_returning_without_a_decision_preserves_unknown_and_never_retries(owner_client, inbox_account):
    reply = uncertain(inbox_account)
    with patch("apps.inbox.services._dispatch_to_platform") as provider:
        page = owner_client.get(review_url(reply))
        response = owner_client.get(detail_url(reply.inbox_message))
    provider.assert_not_called()
    reply.refresh_from_db()
    assert page.status_code == response.status_code == 200
    assert b"Keep the current outcome and return" in page.content
    assert reply.status == "unknown" and not reply.not_sent_verified
    assert not InternalNote.objects.exists()


def test_managed_reply_has_no_override_form_and_post_remains_held(owner_client, inbox_account):
    reply = uncertain(inbox_account)
    DMSendControl.objects.create(
        social_account=inbox_account,
        workspace=inbox_account.workspace,
        platform=inbox_account.platform,
        account_platform_id=inbox_account.account_platform_id,
        coverage_from=timezone.now(),
        coverage_version="brightbean-dm-gate-v1",
    )
    page = owner_client.get(review_url(reply))
    assert b"Record verified result" not in page.content
    assert b"managed send controls" in page.content
    response = owner_client.post(review_url(reply), evidence(reply))
    reply.refresh_from_db()
    assert response.status_code == 409 and reply.status == "unknown"
    assert not InternalNote.objects.exists()
