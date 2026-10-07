"""Fresh regressions for the history/drawer POST rendering boundary."""

import pytest

from .models import EventType, Notification

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "target,template",
    [
        ("notification-history-list", "notifications/partials/history_list.html"),
        ("notification-drawer-content", "notifications/partials/drawer.html"),
    ],
)
def test_mark_all_read_htmx_returns_correct_partial(client, user, target, template):
    client.force_login(user)
    notification = Notification.objects.create(user=user, event_type=EventType.POST_APPROVED, title="Synthetic event")
    response = client.post("/notifications/mark-all-read/", HTTP_HX_REQUEST="true", HTTP_HX_TARGET=target)
    assert response.status_code == 200
    assert template in [item.name for item in response.templates]
    notification.refresh_from_db()
    assert notification.is_read


def test_get_only_drawer_still_rejects_post(client, user):
    client.force_login(user)
    assert client.post("/notifications/drawer/").status_code == 405
