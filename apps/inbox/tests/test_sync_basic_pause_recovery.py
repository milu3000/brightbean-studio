"""Operational capture pause keeps one authority and the plain saved-history UI."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.durable_sync import claim_page, commit_page
from apps.inbox.models import ConversationMessage, ConversationReadState, InboxMessage, InboxSyncReceipt
from apps.inbox.sync_contracts import SyncPage
from apps.inbox.sync_identity import SyncError, canonical_owns_account
from apps.inbox.sync_ingestion import enqueue_message, process_receipt
from apps.inbox.sync_scheduler import run_sync_cycle
from apps.inbox.tasks import InboxSyncEngine
from apps.inbox.tests.test_durable_pages_recovery import checkpoint, message
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable
from apps.inbox.webhooks import _create_if_new, _handle_facebook_messaging
from apps.members.models import WorkspaceMembership

durable = _durable
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("pause_actions", [False, True])
def test_capture_pause_preserves_cursor_queue_and_basic_reads_without_legacy_writer(
    durable, settings, client, user, org_owner, enroll_conversation_accounts, pause_actions
):
    account = durable.social_account
    enroll_conversation_accounts(account, read=True)
    settings.INBOX_CANONICAL_READ_ENABLED = True
    WorkspaceMembership.objects.create(user=user, workspace=account.workspace, workspace_role="owner")
    cp = checkpoint(durable)
    lease = claim_page(cp.pk)
    saved = message(lease, body="PAUSED SAVED CANONICAL")
    commit_page(lease, SyncPage((saved,), "resumeA", False))
    row = ConversationMessage.objects.get()
    pending = enqueue_message(
        account, replace(saved, platform_message_id="pending", conversation_id="", source="webhook")
    )
    assert pending.status == "awaiting_identity"
    old_page = claim_page(cp.pk)
    before_checkpoints = list(durable.checkpoints.values())
    before_receipts = list(InboxSyncReceipt.objects.values())
    before_messages = list(ConversationMessage.objects.values())
    settings.INBOX_DURABLE_SYNC_ENABLED = False
    settings.INBOX_CONVERSATION_WORKFLOW_ENABLED = not pause_actions
    settings.INBOX_CONVERSATION_COMPOSER_ENABLED = not pause_actions
    assert canonical_owns_account(account)
    with pytest.raises(SyncError, match="revoked"):
        commit_page(old_page, SyncPage((message(old_page, mid="inflight"),)))
    with pytest.raises(SyncError, match="revoked"):
        process_receipt(pending.pk)
    legacy = SimpleNamespace(
        platform_message_id=saved.platform_message_id,
        message_type="dm",
        sender_name="Peer",
        sender_id="peer-1",
        text="STALE POLL",
        timestamp=timezone.now(),
        extra={},
    )
    InboxSyncEngine()._upsert_message(account, legacy)
    _create_if_new(account, saved.platform_message_id, "dm", "Peer", "peer-1", "STALE WEBHOOK", {})
    _handle_facebook_messaging(
        account,
        {
            "sender": {"id": "peer-1"},
            "recipient": {"id": "page-1"},
            "message": {"mid": "new-paused-delivery", "text": "NO CAPTURE"},
            "timestamp": int(timezone.now().timestamp() * 1000),
        },
    )
    client.force_login(user)
    with (
        patch("apps.inbox.meta_sync_adapter.MetaSyncAdapter.fetch") as native_fetch,
        patch("apps.inbox.native_thread_reads.read_native_thread") as overlay,
    ):
        assert run_sync_cycle() == {"pages": 0, "held": 0}
        feed = client.get(reverse("inbox:basic_feed", kwargs={"workspace_id": account.workspace_id}), secure=True)
        detail = client.get(
            reverse(
                "inbox:basic_detail",
                kwargs={"workspace_id": account.workspace_id, "conversation_id": row.conversation_id},
            ),
            secure=True,
        )
    assert feed.status_code == detail.status_code == 200
    assert "PAUSED SAVED CANONICAL" in detail.content.decode()
    native_fetch.assert_not_called()
    overlay.assert_not_called()
    assert not InboxMessage.objects.exists() and not ConversationReadState.objects.exists()
    assert list(ConversationMessage.objects.values()) == before_messages
    assert list(durable.checkpoints.values()) == before_checkpoints
    assert list(InboxSyncReceipt.objects.values()) == before_receipts
