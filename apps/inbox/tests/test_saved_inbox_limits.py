"""Saved-target pagination uses one REST contract across both stored sources."""

from unittest.mock import patch

import pytest

from apps.api.api import api
from apps.inbox.inbox_intents import read_inbox
from apps.inbox.tests.test_inbox_intents import composer as composer
from apps.inbox.tests.test_inbox_intents import intent_context as intent_context
from apps.inbox.tests.test_inbox_intents import surface as surface
from apps.inbox.tests.test_legacy_intents import context as context
from apps.inbox.tests.test_legacy_intents import public as public

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("source", ["canonical", "legacy"])
@pytest.mark.parametrize("limit", [1, 50, 0, 51, 100, 101])
def test_saved_target_rest_limit_is_independent_of_source(request, source, limit):
    if source == "canonical":
        owner, client, _key = request.getfixturevalue("intent_context")
        target_id = owner.conversation.pk
    else:
        client, message, _reference = request.getfixturevalue("public")
        target_id = message.pk
    with patch("apps.api.routers.saved_inbox.read_inbox", wraps=read_inbox) as read:
        response = client.get(f"/api/v1/inbox/saved/targets/{target_id}", {"limit": limit}, secure=True)
    if limit in (1, 50):
        assert response.status_code == 200, response.content
        assert response.json()["conversation"]["limit"] == limit
        assert read.call_count == 1
    else:
        assert response.status_code == 422, response.content
        assert any(error["loc"] == ["query", "limit"] for error in response.json()["detail"])
        read.assert_not_called()


def test_saved_target_openapi_bounds_leave_canonical_api_unchanged():
    paths = api.get_openapi_schema()["paths"]
    for path, maximum in (
        ("/api/v1/inbox/saved/targets/{target_id}", 50),
        ("/api/v1/inbox-conversations/{conversation_id}", 100),
    ):
        limit = next(item for item in paths[path]["get"]["parameters"] if item["name"] == "limit")
        assert limit["schema"]["minimum"] == 1
        assert limit["schema"]["maximum"] == maximum
        assert limit["schema"]["default"] == 30
