"""A verified optional-field failure changes only the next budgeted page."""

from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from django.utils import timezone

from apps.inbox.durable_sync import claim_page, commit_page, run_one_page
from apps.inbox.meta_sync_adapter import MetaSyncAdapter
from apps.inbox.models import ConversationMessage, InboxSyncBudget
from apps.inbox.sync_contracts import SyncPage
from apps.inbox.sync_scheduler import release_gets, reserve_gets
from apps.inbox.tests.test_durable_pages_recovery import checkpoint, message
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable
from apps.inbox.tests.test_meta_sync_recovery import metadata

durable = _durable
pytestmark = pytest.mark.django_db


def unsupported():
    return {"error": {"code": 100, "message": "Tried accessing nonexisting field (shares) on node type (Message)"}}


def test_optional_field_fallback_keeps_cursor_media_and_get_budget_then_recovers(durable):
    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    occurred = timezone.now() - timedelta(minutes=1)
    commit_page(
        lease,
        SyncPage(
            (message(lease, attachments=({"type": "image", "url": "https://scontent.xx.fbcdn.net/original.jpg"},)),),
            "cursorA",
            False,
        ),
    )
    row = ConversationMessage.objects.get()
    original_attachments = row.attachments
    assert original_attachments
    requests = []
    recover = False

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/thread-1"):
            return httpx.Response(200, json=metadata())
        assert request.url.params["after"] in {"cursorA", "cursorB"}
        extended = "attachments" in request.url.params["fields"]
        if extended and not recover:
            return httpx.Response(400, json=unsupported())
        data = {
            "data": [
                {
                    "id": "m-1",
                    "from": {"id": "peer-1"},
                    "to": {"data": [{"id": "page-1"}]},
                    "created_time": occurred.isoformat(),
                    "message": "Basic text" if not extended else "Full text",
                }
            ]
        }
        if not extended:
            data["paging"] = {
                "cursors": {"after": "cursorB"},
                "next": "https://graph.facebook.com/v25.0/thread-1/messages?after=cursorB",
            }
        return httpx.Response(200, json=data)

    adapter = MetaSyncAdapter(transport=httpx.MockTransport(respond))
    with patch("apps.inbox.sync_scheduler._resolve_publish_credentials", return_value={"client_id": "synthetic"}):
        reservation = reserve_gets(durable.pk, 2)
        result = run_one_page(cp.pk, adapter)
        release_gets(reservation)
        assert len(requests) == 2 and result.status == "retry"
        assert result.cursor == "cursorA" and result.pages_committed == 1
        assert result.content_fields_mode == "basic" and result.content_probe_after > timezone.now()
        assert result.last_error_code == "content_fields_unavailable"
        assert run_one_page(cp.pk, adapter) is None and len(requests) == 2
        # Simulate the later budgeted cycle, without sleeping or performing I/O.
        type(cp).objects.filter(pk=cp.pk).update(retry_at=None)
        reservation = reserve_gets(durable.pk, 2)
        result = run_one_page(cp.pk, adapter)
        release_gets(reservation)
        assert len(requests) == 4 and InboxSyncBudget.objects.get().gets_reserved == 4
        assert reserve_gets(durable.pk, 1) is None
        assert result.cursor == "cursorB" and result.content_fields_mode == "basic"
        row.refresh_from_db()
        assert row.body == "Basic text" and row.attachments == original_attachments
        assert row.content_status == "fields_unavailable" and row.observation_state.repair_required
        # A later full-capability probe uses its own reserved two GETs. It may
        # authoritatively remove previously saved media only after full success.
        recover = True
        type(cp).objects.filter(pk=cp.pk).update(content_probe_after=timezone.now() - timedelta(seconds=1))
        InboxSyncBudget.objects.update(window_started_at=timezone.now() - timedelta(minutes=6))
        reservation = reserve_gets(durable.pk, 2)
        result = run_one_page(cp.pk, adapter)
        release_gets(reservation)
        assert len(requests) == 6 and result.content_fields_mode == "extended" and result.status == "complete"
        row.refresh_from_db()
        assert row.body == "Full text" and row.attachments == [] and row.content_status == "text"
        assert not row.observation_state.repair_required


@pytest.mark.parametrize(
    "status,error,expected",
    [
        (403, unsupported(), "permission_unavailable"),
        (
            400,
            {
                "error": {
                    "code": 100,
                    "message": "Unsupported get request. Object cannot be loaded due to missing permissions.",
                }
            },
            "invalid_response",
        ),
        (
            400,
            {"error": {"code": 100, "message": "Tried accessing nonexisting field (to) on node type (Message)"}},
            "invalid_response",
        ),
        (
            400,
            {"error": {"code": 190, "message": "Tried accessing nonexisting field (shares)"}},
            "permission_unavailable",
        ),
    ],
)
def test_permissions_unknown_errors_and_identity_fields_never_downgrade(durable, status, error, expected):
    cp = checkpoint(durable)
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path.endswith("/thread-1"):
            return httpx.Response(200, json=metadata())
        return httpx.Response(status, json=error)

    result = run_one_page(cp.pk, MetaSyncAdapter(transport=httpx.MockTransport(respond)))
    assert len(seen) == 2 and result.last_error_code == expected
    assert result.content_fields_mode == "extended" and result.status == "blocked"
    assert not ConversationMessage.objects.exists()


def test_optional_field_like_error_on_native_participants_does_not_downgrade(durable):
    result = run_one_page(
        checkpoint(durable).pk,
        MetaSyncAdapter(transport=httpx.MockTransport(lambda request: httpx.Response(400, json=unsupported()))),
    )
    assert result.status == "blocked" and result.last_error_code == "invalid_response"
    assert result.content_fields_mode == "extended"


def test_basic_exhaustion_requires_explicit_partial_cutover_review(durable):
    from apps.inbox.sync_cutover import establish_cutover, preview_cutover
    from apps.inbox.sync_identity import SyncError
    from apps.inbox.tests.test_sync_cutover_recovery import bootstrap

    cp = bootstrap(durable)
    type(cp).objects.filter(pk=cp.pk).update(
        content_fields_mode="basic", content_probe_after=timezone.now() + timedelta(hours=6)
    )
    preview = preview_cutover(durable.pk)
    assert str(cp.pk) in preview["partial_checkpoint_ids"]
    with pytest.raises(SyncError, match="partial_coverage"):
        establish_cutover(
            durable.pk, expected_fingerprint=preview["fingerprint"], workflow_mapping=preview["suggested_mapping"]
        )


def test_capability_change_fences_an_old_page_before_provider_io(durable):
    from apps.inbox.sync_identity import SyncError

    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    type(cp).objects.filter(pk=cp.pk).update(content_fields_mode="basic")
    seen = []
    adapter = MetaSyncAdapter(transport=httpx.MockTransport(lambda request: seen.append(request)))
    with pytest.raises(SyncError, match="lease_lost"):
        adapter.fetch(lease)
    assert seen == []


def test_unavailable_full_response_does_not_clear_partial_content_marker(durable):
    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    commit_page(lease, SyncPage((message(lease, content_fetch_status="basic_fallback", attachments_complete=False),)))
    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    commit_page(
        lease,
        SyncPage(
            (
                message(
                    lease,
                    body="",
                    content_available=False,
                    content_fetch_status="fields_requested",
                    snapshot_started_at=timezone.now(),
                ),
            )
        ),
    )
    row = ConversationMessage.objects.get()
    assert row.body == "Hello" and row.content_status == "fields_unavailable"
    assert row.observation_state.repair_required
