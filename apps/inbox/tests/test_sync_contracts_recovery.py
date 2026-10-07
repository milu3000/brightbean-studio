"""Fresh reconstruction tests, not claims about the lost source tree."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.inbox.models import InboxSyncConnection
from apps.inbox.sync_contracts import ConversationObservation, SyncPage, calendar_months, validate_page
from apps.inbox.sync_identity import SyncError, canonical_owns_account, canonical_read_connection, classify_participants

pytestmark = pytest.mark.django_db


@pytest.fixture
def binding(inbox_account):
    return InboxSyncConnection.objects.create(
        social_account=inbox_account,
        workspace=inbox_account.workspace,
        platform=inbox_account.platform,
        account_platform_id=inbox_account.account_platform_id,
        auth_fingerprint="0" * 64,
    )


def test_empty_default_off_binding_is_not_capture_or_legacy_ownership(binding):
    assert not binding.enabled and not canonical_owns_account(binding.social_account)


def test_persisted_ownership_survives_generation_rotation_and_pause(binding):
    binding.ownership_claimed_at = timezone.now()
    binding.generation = uuid.uuid4()
    binding.save()
    assert canonical_owns_account(binding.social_account)


def test_saved_history_native_identity_does_not_depend_on_current_token(binding):
    account = binding.social_account
    account.oauth_access_token = "synthetic-rotated"
    account.save(update_fields=["oauth_access_token"])
    assert canonical_read_connection(account).pk == binding.pk
    account.account_platform_id = "another-native-account"
    account.save(update_fields=["account_platform_id"])
    with pytest.raises(SyncError, match="provenance"):
        canonical_read_connection(account)


def test_native_participant_set_classification_does_not_require_invented_message_sender(binding):
    assert classify_participants(binding.social_account, ("page-1", "peer-1")) == (
        "direct",
        "participants_pair",
        "peer-1",
    )
    assert classify_participants(binding.social_account, ("page-1", "peer-1", "peer-2"))[0] == "group"


def test_calendar_months_clip_calendar_day_not_180_days():
    assert calendar_months(datetime(2024, 8, 31, tzinfo=UTC), 6) == datetime(2025, 2, 28, tzinfo=UTC)


def test_entire_provider_page_is_validated_before_any_write():
    lease = SimpleNamespace(stream="conversations", claimed_at=timezone.now() - timedelta(seconds=1))
    item = ConversationObservation("thread", ("page-1", "peer-1"))
    with pytest.raises(SyncError, match="duplicate_page_identity"):
        validate_page(SyncPage((item, item)), lease, timezone.now())
    with pytest.raises(SyncError, match="invalid_page"):
        validate_page(SyncPage((item,) * 101), lease, timezone.now())
    with pytest.raises(SyncError, match="invalid_page"):
        validate_page(SyncPage((item,), "https://untrusted.test/next", False), lease, timezone.now())


def test_saved_history_never_decrypts_provider_credentials(binding):
    account = binding.social_account
    with patch("apps.common.encryption.decrypt_value", side_effect=ValueError("synthetic corruption")):
        assert canonical_read_connection(account).pk == binding.pk
