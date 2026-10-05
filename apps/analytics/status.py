"""Safe, independent analytics availability and account authorization evidence.

No raw exception text, response bodies, tokens, URLs or user notes are retained.
Publishing state is never changed. User-confirmed visibility wins over polling.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.composer.models import PlatformPost
from apps.social_accounts.models import SocialAccount

_ACCOUNT_CATEGORIES = {"account_auth", "account_scope"}
_LABELS = {
    "unknown": "Not yet verified",
    "available": "Available",
    "inaccessible": "Post unavailable; archive or deletion not confirmed",
    "archived": "Archived",
    "deleted": "Deleted",
}


def post_analytics_status(post) -> dict:
    label = _LABELS.get(post.analytics_availability, _LABELS["unknown"])
    if post.analytics_availability not in {"archived", "deleted"}:
        if post.analytics_error_category == "transient":
            label = "Temporarily unable to refresh metrics"
        elif post.analytics_error_category == "unknown":
            label = "Metrics fetch failed; cause unverified"
        elif post.analytics_error_category in _ACCOUNT_CATEGORIES:
            label = "Metrics need account authorization"
    return {
        "availability": post.analytics_availability,
        "label": label,
        "source": post.analytics_availability_source,
        "checked_at": post.analytics_availability_checked_at,
        "error_category": post.analytics_error_category,
        "error_evidence": post.analytics_error_evidence,
        "attempted_at": post.analytics_attempted_at,
        "version": post.analytics_status_version,
    }


def account_analytics_status(account) -> dict:
    verified = account.analytics_needs_reconnect and account.analytics_reconnect_reason in _ACCOUNT_CATEGORIES
    return {
        "needs_reconnect": bool(verified),
        "verification_pending": bool(account.analytics_needs_reconnect and not verified),
        "category": account.analytics_reconnect_reason,
        "context": account.analytics_reconnect_context,
        "checked_at": account.analytics_reconnect_checked_at,
        "evidence": account.analytics_reconnect_evidence,
    }


def record_account_failure(account, classification, *, context, checked_at=None):
    """Only reliable account evidence may set reconnect. Fence old OAuth calls."""
    if not classification.is_account_error:
        return False
    checked_at = checked_at or timezone.now()
    candidates = SocialAccount.objects.filter(
        pk=account.pk, analytics_auth_updated_at=account.analytics_auth_updated_at
    ).filter(Q(analytics_reconnect_checked_at__isnull=True) | Q(analytics_reconnect_checked_at__lte=checked_at))
    with transaction.atomic():
        current = (
            candidates.select_for_update()
            .only("analytics_needs_reconnect", "analytics_reconnect_reason", "analytics_reconnect_context")
            .first()
        )
        if current is None:
            return False
        sticky = (
            current.analytics_needs_reconnect
            and current.analytics_reconnect_reason == "account_scope"
            and current.analytics_reconnect_context == "account"
            and (classification.category != "account_scope" or context != "account")
        )
        values = {"analytics_needs_reconnect": True, "analytics_reconnect_checked_at": checked_at}
        if not sticky:
            values.update(
                analytics_reconnect_reason=classification.category,
                analytics_reconnect_context=context,
                analytics_reconnect_evidence=classification.safe_evidence,
            )
        # Even a weaker failure advances the fence. An older in-flight success
        # must not clear a newer failure while its stronger context is retained.
        updated = SocialAccount.objects.filter(pk=current.pk).update(**values)
    # Keep the evidence loaded at pass start, so a later success in this same
    # mixed-result pass cannot erase the failure that just landed in the DB.
    if updated:
        account.analytics_needs_reconnect = True
    return bool(updated)


def record_account_success(account, *, context):
    """Recover only an observed older flag whose failing surface was verified.

    A post success does not prove account-level scopes. Legacy flags recover
    on account insights success, or Threads post insights (its only analytics
    endpoint). No migration guesses that an old flag was false.
    """
    if not account.analytics_needs_reconnect:
        return False
    reason = account.analytics_reconnect_reason
    if reason == "account_scope" and account.analytics_reconnect_context != context:
        return False
    if not reason and context != "account" and account.platform != "threads":
        return False
    updated = SocialAccount.objects.filter(
        pk=account.pk,
        analytics_auth_updated_at=account.analytics_auth_updated_at,
        analytics_reconnect_checked_at=account.analytics_reconnect_checked_at,
        analytics_reconnect_reason=reason,
        analytics_needs_reconnect=True,
    ).update(
        analytics_needs_reconnect=False,
        analytics_reconnect_reason="",
        analytics_reconnect_context="",
        analytics_reconnect_checked_at=None,
        analytics_reconnect_evidence={},
    )
    if updated:
        account.analytics_needs_reconnect = False
    return bool(updated)


def record_post_observation(post_ids, *, checked_at=None, classification=None):
    """Write only analytics fields; preserve user confirmation and newer polls."""
    if not post_ids:
        return
    checked_at = checked_at or timezone.now()
    qs = (
        PlatformPost.objects.filter(pk__in=post_ids)
        .filter(Q(analytics_attempted_at__isnull=True) | Q(analytics_attempted_at__lte=checked_at))
        .filter(
            Q(analytics_availability_checked_at__isnull=True) | Q(analytics_availability_checked_at__lte=checked_at)
        )
    )
    category = classification.category if classification else ""
    evidence = classification.safe_evidence if classification else {}
    with transaction.atomic():
        # Lock to prevent a user confirmation racing the two selective updates.
        ids = list(qs.select_for_update().values_list("pk", flat=True))
        if not ids:
            return
        target = PlatformPost.objects.filter(pk__in=ids)
        target.update(
            analytics_attempted_at=checked_at,
            analytics_failure_count=F("analytics_failure_count") + 1 if classification else 0,
            analytics_error_category=category,
            analytics_error_evidence=evidence,
            analytics_status_version=F("analytics_status_version") + 1,
        )
        availability = {
            "": "available",
            "post_inaccessible": "inaccessible",
            "post_archived": "archived",
            "post_deleted": "deleted",
        }.get(category)
        if availability:
            target.exclude(analytics_availability_source="user").update(
                analytics_availability=availability,
                analytics_availability_source="platform",
                analytics_availability_checked_at=checked_at,
            )


def confirm_post_availability(post, *, availability, expected_version, confirmed):
    """Explicit local annotation only; never archives/deletes the remote post."""
    if confirmed is not True:
        raise ValueError("Explicit confirmation of the post's platform status is required.")
    if availability not in {"archived", "deleted", "unknown"}:
        raise ValueError("Choose archived, deleted, or unknown (remove the confirmation).")
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("expected_version must be a non-negative integer.")
    updated = PlatformPost.objects.filter(pk=post.pk, analytics_status_version=expected_version).update(
        analytics_availability=availability,
        analytics_availability_source="user" if availability != "unknown" else "",
        analytics_availability_checked_at=timezone.now(),
        analytics_status_version=F("analytics_status_version") + 1,
    )
    if not updated:
        raise ValueError("The status changed. Reload the post before confirming again.")
    post.refresh_from_db()
    return post_analytics_status(post)
