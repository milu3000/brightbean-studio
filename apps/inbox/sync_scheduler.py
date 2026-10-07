"""The existing five-minute worker advances durable pages under shared limits.

These are engineering budgets, not promised freshness: 4 GET/account/window,
10 GET/app/window, at most two reserved accounts per app. Shared provider quota
breakers may further restrict them. No extra queue service or activation.
"""

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.common import quota
from apps.publisher.engine import _resolve_publish_credentials

from .durable_sync import LEASE_SECONDS, claim_page, commit_page, fail_page, lock_connection, start_scan
from .meta_sync_adapter import MetaSyncAdapter
from .models import InboxSyncBudget, InboxSyncConnection
from .sync_identity import SyncError
from .sync_ingestion import drain_receipts

WINDOW_SECONDS = 300
ACCOUNT_GETS = 4
APP_GETS = 10
MAX_ACCOUNTS = 2


@transaction.atomic
def reserve_gets(connection_id, count, *, now=None):
    now = now or timezone.now()
    if isinstance(count, bool) or count not in {1, 2}:
        raise SyncError("invalid_budget")
    account, connection = lock_connection(connection_id)
    if connection.blocked_reason or (connection.retry_at and connection.retry_at > now):
        return None
    credential = quota.credential_key(_resolve_publish_credentials(account))
    if quota.quota_blocked_until(account.platform, credential, quota.read_scope(account.platform)):
        return None
    app_key = f"{account.platform}:{credential}"
    InboxSyncBudget.objects.get_or_create(app_key=app_key, defaults={"window_started_at": now})
    budget = InboxSyncBudget.objects.select_for_update().get(pk=app_key)
    if now >= budget.window_started_at + timedelta(seconds=WINDOW_SECONDS):
        budget.window_started_at, budget.gets_reserved, budget.account_spend = now, 0, {}
    # Clock rollback cannot reset counters. Absolute expirations conservatively
    # retain active slots; crashed workers release automatically after 90 seconds.
    live = [slot for slot in budget.active_leases if slot["expires"] > now.timestamp()]
    key = str(account.pk)
    spent = budget.account_spend.get(key, 0)
    if any(slot["account"] == key for slot in live) or len(live) >= MAX_ACCOUNTS:
        return None
    if budget.gets_reserved + count > APP_GETS or spent + count > ACCOUNT_GETS:
        return None
    token = str(uuid.uuid4())
    live.append({"token": token, "account": key, "expires": (now + timedelta(seconds=LEASE_SECONDS)).timestamp()})
    budget.active_leases = live
    budget.gets_reserved += count
    budget.account_spend[key] = spent + count
    budget.save()
    return app_key, token


@transaction.atomic
def release_gets(reservation):
    if reservation is None:
        return
    app_key, token = reservation
    budget = InboxSyncBudget.objects.select_for_update().get(pk=app_key)
    budget.active_leases = [slot for slot in budget.active_leases if slot["token"] != token]
    # Counters are deliberately not refunded: a crash/timeout may have used GETs.
    budget.save(update_fields=["active_leases", "updated_at"])


def prepare_scans(connection, now):
    context = "live" if connection.bootstrap_baseline_at else "bootstrap"
    listing = connection.checkpoints.filter(context=context, stream="conversations").first()
    if listing is None or (
        context == "live" and listing.status == "complete" and listing.scan_started_at <= now - timedelta(minutes=5)
    ):
        start_scan(connection.pk, context=context, now=now)
    if connection.bootstrap_baseline_at:
        repair = connection.checkpoints.filter(context="repair", stream="conversations").first()
        if repair is None or (repair.status == "complete" and repair.scan_started_at <= now - timedelta(days=1)):
            start_scan(connection.pk, context="repair", now=now)


def _next_page(connection, *, served_live=False, now):
    due = connection.checkpoints.filter(
        connection_generation=connection.generation, status__in=["ready", "retry", "running"]
    )
    due = due.filter(Q(retry_at__isnull=True) | Q(retry_at__lte=now)).filter(
        Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)
    )
    # Each live cycle yields a turn to a progressive repair after one current
    # page. Native active message edges take turns with discovery pages.
    contexts = ["repair", "live"] if served_live else ["live", "repair"]
    contexts += ["bootstrap", "backfill"]
    for context in contexts:
        candidate = (
            due.filter(context=context)
            .order_by("pages_committed", F("last_committed_at").asc(nulls_first=True), "stream", "pk")
            .first()
        )
        if candidate is not None:
            return candidate
    return None


def run_sync_cycle(*, adapter=None, now=None):
    if getattr(settings, "INBOX_DURABLE_SYNC_ENABLED", False) is not True:
        return {"pages": 0, "held": 0}
    now, adapter = now or timezone.now(), adapter or MetaSyncAdapter()
    ids = list(
        InboxSyncConnection.objects.filter(enabled=True)
        .order_by(F("last_served_at").asc(nulls_first=True), "pk")
        .values_list("pk", flat=True)
    )
    result = {"pages": 0, "held": 0}
    served_live = set()
    # Four fair rounds are sufficient for each account's configured GET cap.
    for round_number in range(ACCOUNT_GETS):
        for connection_id in ids:
            reservation = None
            try:
                connection = InboxSyncConnection.objects.get(pk=connection_id)
                if round_number == 0:
                    drain_receipts(connection_id)
                    prepare_scans(connection, now)
                checkpoint = _next_page(connection, served_live=connection_id in served_live, now=now)
                if checkpoint is None:
                    continue
                reservation = reserve_gets(connection_id, adapter.required_gets(checkpoint.stream))
                if reservation is None:
                    result["held"] += 1
                    continue
                lease = claim_page(checkpoint.pk)
                if lease is None:
                    continue
                try:
                    commit_page(lease, adapter.fetch(lease))
                    drain_receipts(connection_id)
                    result["pages"] += 1
                    if lease.context == "live":
                        served_live.add(connection_id)
                except SyncError as error:
                    if error.code not in {"lease_lost", "enrollment_or_identity_revoked", "generation_changed"}:
                        fail_page(lease, error)
                    result["held"] += 1
            except SyncError:
                result["held"] += 1
            finally:
                release_gets(reservation)
    return result


def public_messages_only(account, provider, since):
    """Retain public-comment polling without calling the combined DM writer."""
    if account.platform == "facebook":
        return provider._fetch_post_comments(account.oauth_access_token, since)
    if account.platform == "instagram_login":
        return provider._fetch_media_comments(account.oauth_access_token, since)
    return []
