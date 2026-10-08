"""Reconstructed normalized queue, signed tombstone and legacy cutover tests."""
# ruff: noqa: F811

from dataclasses import replace
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.inbox.durable_sync import auth_fingerprint, claim_page, commit_page
from apps.inbox.models import ConversationMessage, InboxMessage, InboxSyncReceipt
from apps.inbox.sync_contracts import SyncPage
from apps.inbox.sync_ingestion import drain_receipts, enqueue_message
from apps.inbox.tests.test_durable_pages_recovery import checkpoint, durable, message  # noqa: F401
from apps.inbox.webhooks import _handle_facebook_messaging, _process_meta_events

pytestmark = pytest.mark.django_db


def item(binding, mid="m-1"):
    lease = claim_page(checkpoint(binding).pk)
    return replace(message(lease, mid=mid), source="webhook"), lease


def messaging(mid="m-1", *, deleted=False):
    return {
        "sender": {"id": "peer-1"},
        "recipient": {"id": "page-1"},
        "timestamp": int(timezone.now().timestamp() * 1000),
        "message": {"mid": mid, "is_deleted": deleted, "text": "Ignored on deletion" if deleted else "Hello"},
    }


def instagram(binding):
    account = binding.social_account
    account.platform = "instagram_login"
    account.save()
    binding.platform = account.platform
    binding.auth_fingerprint = auth_fingerprint(account)
    binding.save()
    return account


def signed_delivery(account, mid="m-1", *, object_name="instagram", secret="synthetic-secret"):
    payload = {
        "object": object_name,
        "entry": [{"id": account.account_platform_id, "messaging": [messaging(mid, deleted=True)]}],
    }
    with patch("apps.inbox.webhooks.resolve_app_secret", return_value="synthetic-secret"):
        _process_meta_events(payload, [account.platform], {secret})


def test_pending_identity_never_creates_sender_pair_thread(durable):
    observation, lease = item(durable)
    receipt = enqueue_message(durable.social_account, replace(observation, conversation_id=""))
    assert receipt.status == "awaiting_identity" and not ConversationMessage.objects.exists()
    assert InboxSyncReceipt.objects.count() == 1
    enqueue_message(durable.social_account, replace(observation, conversation_id="", observed_at=timezone.now()))
    assert InboxSyncReceipt.objects.count() == 1
    commit_page(lease, SyncPage((replace(observation, source="poll"),)))
    assert drain_receipts(durable.pk) == 1
    receipt.refresh_from_db()
    assert receipt.status == "processed" and receipt.payload == {}
    assert ConversationMessage.objects.count() == 1 and not InboxMessage.objects.exists()


def test_receipt_enqueue_and_canonical_commit_roll_back_together(durable):
    observation, _lease = item(durable)
    with (
        patch("apps.inbox.sync_observations.reduce_observation", side_effect=RuntimeError("crash")),
        pytest.raises(RuntimeError),
    ):
        enqueue_message(durable.social_account, observation)
    assert not InboxSyncReceipt.objects.exists() and not ConversationMessage.objects.exists()
    receipt = enqueue_message(durable.social_account, observation)
    assert receipt.status == "processed" and ConversationMessage.objects.count() == 1
    enqueue_message(durable.social_account, observation)
    assert InboxSyncReceipt.objects.count() == ConversationMessage.objects.count() == 1


def test_signed_instagram_withdrawal_retains_saved_original_and_redacts(durable, enroll_conversation_accounts):
    account = instagram(durable)
    enroll_conversation_accounts(account)
    observation, lease = item(durable)
    commit_page(lease, SyncPage((replace(observation, source="poll", body="Saved original"),)))
    signed_delivery(account)
    row = ConversationMessage.objects.get()
    state = row.observation_state
    assert row.is_deleted and row.body == "" and state.retained_body == "Saved original"
    deadline = state.expires_at
    signed_delivery(account)
    state.refresh_from_db()
    assert state.expires_at == deadline and state.retained_body == "Saved original"
    assert InboxSyncReceipt.objects.filter(kind="instagram_deleted").count() == 1


def test_unseen_signed_tombstone_prevents_later_backfill_resurrection(durable, enroll_conversation_accounts):
    account = instagram(durable)
    enroll_conversation_accounts(account)
    signed_delivery(account)
    tombstone = ConversationMessage.objects.get()
    assert tombstone.is_deleted and tombstone.conversation_id is None and tombstone.occurred_at is None
    assert tombstone.body == tombstone.observation_state.retained_body == ""
    observation, lease = item(durable)
    commit_page(lease, SyncPage((replace(observation, source="poll", body="Never captured before deletion"),)))
    tombstone.refresh_from_db()
    assert tombstone.conversation_id and tombstone.is_deleted and tombstone.body == ""
    assert tombstone.observation_state.retained_body == "" and ConversationMessage.objects.count() == 1


@pytest.mark.parametrize("case", ["wrong_app", "wrong_object", "unsigned_handler", "facebook"])
def test_only_exact_signed_instagram_login_contract_deletes(durable, enroll_conversation_accounts, case):
    account = durable.social_account if case == "facebook" else instagram(durable)
    enroll_conversation_accounts(account)
    if case == "unsigned_handler":
        _handle_facebook_messaging(account, messaging(deleted=True))
    else:
        signed_delivery(
            account,
            secret="wrong" if case == "wrong_app" else "synthetic-secret",
            object_name="page" if case == "wrong_object" else "instagram",
        )
    assert not ConversationMessage.objects.exists() and not InboxSyncReceipt.objects.exists()


def test_disconnected_bootstrap_ownership_excludes_legacy_writer_after_reconnect(durable):
    from apps.inbox.tasks import InboxSyncEngine
    from apps.inbox.webhooks import _create_if_new
    from providers.base import InboxMessage as ProviderMessage

    observation, lease = item(durable)
    commit_page(lease, SyncPage((replace(observation, source="poll", body="Canonical current"),)))
    durable.enabled = False
    durable.save()
    account = durable.social_account
    account.connection_status = "disconnected"
    account.save()
    account.connection_status = "connected"
    account.save()
    msg = ProviderMessage(
        platform_message_id="m-1",
        message_type="dm",
        sender_name="Peer",
        sender_id="peer-1",
        text="Old writer",
        timestamp=timezone.now(),
        extra={},
    )
    InboxSyncEngine()._upsert_message(account, msg)
    _create_if_new(account, "m-1", "dm", "Peer", "peer-1", "Old webhook", {})
    assert ConversationMessage.objects.get().body == "Canonical current"
    assert not InboxMessage.objects.exists()


def test_queued_receipt_cannot_capture_after_revocation(durable):
    observation, _lease = item(durable)
    receipt = enqueue_message(durable.social_account, replace(observation, conversation_id=""))
    durable.enabled = False
    durable.save()
    from apps.inbox.sync_identity import SyncError

    with pytest.raises(SyncError, match="revoked"):
        drain_receipts(durable.pk)
    receipt.refresh_from_db()
    assert receipt.status == "awaiting_identity" and not ConversationMessage.objects.exists()


def test_http_signature_boundary_controls_unseen_tombstone(durable, enroll_conversation_accounts, client, settings):
    import hashlib
    import hmac
    import json

    from django.urls import reverse

    account = instagram(durable)
    enroll_conversation_accounts(account)
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {"instagram_login": {"app_secret": "synthetic-secret"}}
    body = json.dumps(
        {
            "object": "instagram",
            "entry": [{"id": account.account_platform_id, "messaging": [messaging("http-mid", deleted=True)]}],
        }
    ).encode()
    url = reverse("inbox_webhooks:webhook_instagram_login")
    rejected = client.post(url, body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256="sha256=invalid")
    assert rejected.status_code == 403 and not ConversationMessage.objects.exists()
    signature = "sha256=" + hmac.new(b"synthetic-secret", body, hashlib.sha256).hexdigest()
    accepted = client.post(url, body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=signature)
    assert accepted.status_code == 200
    row = ConversationMessage.objects.get(platform_message_id="http-mid")
    assert row.is_deleted and row.body == "" and row.conversation_id is None
