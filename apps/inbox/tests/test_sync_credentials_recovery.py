from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from apps.inbox.durable_sync import auth_fingerprint, claim_page, commit_page
from apps.inbox.sync_contracts import SyncPage
from apps.inbox.sync_identity import SyncError, canonical_read_connection
from apps.inbox.tests.test_durable_pages_recovery import checkpoint, message
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable

pytestmark = pytest.mark.django_db
durable = _durable


def provider():
    result = Mock()
    result.refresh_token.return_value = Mock(
        access_token="new-trusted-token", refresh_token="new-refresh-token", expires_in=3600
    )
    return result


def test_trusted_refresh_keeps_history_generation_but_invalidates_old_page(durable):
    cp = checkpoint(durable)
    old = claim_page(cp.pk)
    account = durable.social_account
    old_generation, old_auth = durable.generation, durable.auth_fingerprint
    account.refresh_oauth_token(provider(), enqueue_backfill=False)
    durable.refresh_from_db()
    cp.refresh_from_db()
    assert durable.generation == old_generation and durable.auth_fingerprint != old_auth
    assert durable.auth_fingerprint == auth_fingerprint(account)
    assert canonical_read_connection(account).generation == old_generation
    assert cp.cursor == "" and cp.lease_token is None
    with pytest.raises(SyncError, match="lease_lost"):
        commit_page(old, SyncPage((message(old),)))
    new = claim_page(cp.pk)
    commit_page(new, SyncPage((message(new),)))


def test_arbitrary_token_change_keeps_saved_reads_but_holds_capture(durable):
    cp = checkpoint(durable)
    account = durable.social_account
    account.oauth_access_token = "unverified-replacement"
    account.save(update_fields=["oauth_access_token"])
    assert canonical_read_connection(account).pk == durable.pk
    with pytest.raises(SyncError, match="revoked"):
        claim_page(cp.pk)


def test_newer_refresh_wins_without_rebinding_stale_result(durable):
    account = durable.social_account
    newer = type(account).objects.get(pk=account.pk)
    newer.analytics_auth_updated_at = timezone.now() + timedelta(seconds=1)
    newer.oauth_access_token = "winning-token"
    newer.save(update_fields=["analytics_auth_updated_at", "oauth_access_token"])
    assert account.refresh_oauth_token(provider(), enqueue_backfill=False) == "winning-token"
    durable.refresh_from_db()
    assert durable.auth_fingerprint != auth_fingerprint(account)


def test_changed_native_owner_cannot_trust_old_refresh(durable):
    account = durable.social_account
    type(account).objects.filter(pk=account.pk).update(account_platform_id="replacement-native")
    account.refresh_oauth_token(provider(), enqueue_backfill=False)
    durable.refresh_from_db()
    assert durable.auth_fingerprint != auth_fingerprint(account)
    with pytest.raises(SyncError, match="provenance"):
        canonical_read_connection(account)
