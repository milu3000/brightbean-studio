"""Projection failure shows trusted acceptance immediately, without body fallback."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.inbox.models import InboxReply
from apps.inbox.tests.test_dispatch_ownership import clock as clock
from apps.inbox.tests.test_owned_composer_bridge import owner as owner

pytestmark = pytest.mark.django_db(transaction=True)


def test_ui_acceptance_survives_missing_projection_and_reopen(owner, client):
    client.force_login(owner.user)
    kwargs = {"workspace_id": owner.account.workspace_id, "conversation_id": owner.conversation.pk}
    detail_url = reverse("inbox:conversation_detail", kwargs=kwargs)
    detail = client.get(detail_url, HTTP_HX_REQUEST="true")
    assert detail.status_code == 200
    state = detail.context["conversation_composer"]
    payload = {
        "body": "Synthetic accepted body pending projection",
        "composer_action_nonce": state["action_nonce"],
        "composer_revision": state["composer_revision"],
        "composer_scope_token": state["scope_token"],
        "composer_observation_token": detail.context["composer_observation_token"],
    }

    def accepted(*args, **kwargs):
        kwargs["before_provider"]()
        return "synthetic-ui-accepted-pending-projection"

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as provider,
        patch("apps.inbox.conversations.record_reply", side_effect=RuntimeError("synthetic projection failure")),
    ):
        response = client.post(reverse("inbox:conversation_send_reply", kwargs=kwargs), payload, HTTP_HX_REQUEST="true")
    assert response.status_code == 200, response.content
    assert provider.call_count == 1
    receipt = InboxReply.objects.get(conversation=owner.conversation)
    assert receipt.status == "sent" and receipt.platform_reply_id == "synthetic-ui-accepted-pending-projection"
    for shown in (response, client.get(detail_url, HTTP_HX_REQUEST="true")):
        assert shown.status_code == 200
        assert shown.context["unprojected_acceptance"] == {"id": str(receipt.pk), "status": "provider_accepted"}
        assert b"Accepted" in shown.content and b"History updating" in shown.content
        assert payload["body"].encode() not in shown.content, "Missing native projection cannot restore a raw body"
