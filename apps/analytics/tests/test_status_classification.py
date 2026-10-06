"""Evidence boundaries, poll races and explicit local archive annotations."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.analytics.freshness import post_freshness
from apps.analytics.models import PostInsightsSnapshot
from apps.analytics.status import (
    account_analytics_status,
    confirm_post_availability,
    post_analytics_status,
    record_account_failure,
    record_account_success,
    record_post_observation,
)
from apps.analytics.tasks import (
    _post_cadence_due,
    _sync_account_metrics,
    _sync_account_posts,
    backfill_account_analytics,
)
from apps.composer.models import PlatformPost, Post
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount
from apps.social_accounts.views import _create_or_update_account
from apps.workspaces.models import Workspace
from providers.analytics_errors import AnalyticsErrorClassification, classify_analytics_error
from providers.exceptions import APIError
from providers.types import AccountMetrics, PostMetrics

pytestmark = pytest.mark.django_db


@pytest.fixture
def account(organization):
    workspace = Workspace.objects.create(name="Analytics evidence", organization=organization)
    AnalyticsPlatformConfig.objects.update_or_create(platform="threads", defaults={"is_enabled": True})
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="threads",
        account_platform_id="test-account",
        account_name="Synthetic",
        oauth_access_token="synthetic-only",
        connection_status="connected",
    )


@pytest.fixture
def post(account):
    parent = Post.objects.create(workspace=account.workspace, caption="Synthetic analytics test")
    return PlatformPost.objects.create(
        post=parent,
        social_account=account,
        platform_post_id="12345",
        published_at=timezone.now() - timedelta(days=1),
        status="published",
    )


def provider(error=None):
    p = Mock(post_metrics_batch_size=1, account_metrics_supports_date_range=False)
    p.get_account_metrics.side_effect = NotImplementedError
    p.get_post_metrics.return_value = PostMetrics(likes=3, impressions=12)
    if error:
        p.get_post_metrics.side_effect = error
    return p


def object_error():
    return APIError(
        "SECRET should never persist",
        status_code=400,
        raw_response={
            "error": {
                "code": 100,
                "error_subcode": 33,
                "message": "Unsupported get request. Object cannot be loaded due to missing permissions; SECRET",
            },
            "access_token": "SECRET",
        },
    )


def test_object_error_does_not_reflag_reconnect_and_history_is_preserved(account, post):
    PostInsightsSnapshot.objects.create(platform_post=post, metric_key="views", date=timezone.now().date(), value=33)
    count = _sync_account_posts(account, provider(object_error()), "unused", [post], timezone.now().date())
    account.refresh_from_db()
    post.refresh_from_db()
    assert count == (0, 1, 1)
    assert not account.analytics_needs_reconnect
    assert post.analytics_availability == "inaccessible"
    assert post.analytics_error_category == "post_inaccessible"
    assert "SECRET" not in str(post.analytics_error_evidence)
    assert post.analytics_availability_source == "platform"
    assert post.analytics_availability_checked_at is not None
    assert PostInsightsSnapshot.objects.get(platform_post=post).value == 33


def test_mixed_old_failure_new_success_does_not_poison_account(account, post):
    newer = PlatformPost.objects.create(
        post=Post.objects.create(workspace=account.workspace),
        social_account=account,
        platform_post_id="newer",
        published_at=timezone.now(),
        status="published",
    )
    p = provider()
    p.get_post_metrics.side_effect = [object_error(), PostMetrics(likes=4)]
    assert _sync_account_posts(account, p, "unused", [post, newer], timezone.now().date()) == (1, 1, 2)
    account.refresh_from_db()
    newer.refresh_from_db()
    assert not account.analytics_needs_reconnect
    assert newer.analytics_availability == "available"


@pytest.mark.parametrize(
    "exc,category",
    [
        (APIError("permission only"), "unknown"),
        (APIError("temporary", status_code=503), "transient"),
    ],
)
def test_unknown_and_transient_never_infer_archive(account, post, exc, category):
    _sync_account_posts(account, provider(exc), "unused", [post], timezone.now().date())
    post.refresh_from_db()
    account.refresh_from_db()
    assert post.analytics_availability == "unknown"
    assert post.analytics_error_category == category
    assert not account.analytics_needs_reconnect


@pytest.mark.parametrize(
    "exc,category",
    [
        (APIError("invalid", raw_response={"error": {"code": 190}}), "account_auth"),
        (
            APIError("scope", raw_response={"error": {"code": 200, "missing_scopes": ["threads_manage_insights"]}}),
            "account_scope",
        ),
    ],
)
def test_real_auth_scope_failure_sets_account_flag_not_post_failure(account, post, exc, category):
    _sync_account_posts(account, provider(exc), "unused", [post], timezone.now().date())
    account.refresh_from_db()
    post.refresh_from_db()
    assert account.analytics_needs_reconnect
    assert account.analytics_reconnect_reason == category
    assert account_analytics_status(account)["needs_reconnect"]
    assert post.analytics_failure_count == 0
    assert post.analytics_attempted_at is None


@pytest.mark.parametrize("state", ["archived", "deleted"])
def test_explicit_platform_state_is_precise_and_pauses_polling(account, post, state):
    exc = APIError("normalized platform state")
    exc.analytics_post_state = state
    _sync_account_posts(account, provider(exc), "unused", [post], timezone.now().date())
    post.refresh_from_db()
    assert post.analytics_availability == state
    assert post.analytics_availability_source == "platform"
    assert not _post_cadence_due(post)
    assert post_freshness(post)[1] is None
    p = provider()
    assert _sync_account_posts(account, p, "unused", [post], timezone.now().date()) == (0, 0, 0)
    p.get_post_metrics.assert_not_called()


def test_user_confirmation_wins_over_polling_and_does_not_change_publishing(post):
    confirm_post_availability(post, availability="archived", expected_version=0, confirmed=True)
    old_confirmed_at = post.analytics_availability_checked_at
    record_post_observation([post.pk], classification=classify_analytics_error(object_error(), "threads"))
    record_post_observation([post.pk])
    post.refresh_from_db()
    assert post.analytics_availability == "archived"
    assert post.analytics_availability_source == "user"
    assert post.analytics_availability_checked_at == old_confirmed_at
    assert post.status == "published"
    with pytest.raises(ValueError, match="status changed"):
        confirm_post_availability(post, availability="deleted", expected_version=0, confirmed=True)
    confirm_post_availability(
        post, availability="unknown", expected_version=post.analytics_status_version, confirmed=True
    )
    record_post_observation([post.pk])
    post.refresh_from_db()
    assert post.analytics_availability == "available"


@pytest.mark.parametrize("confirmed", [False, "true", 1, None])
def test_annotation_requires_actual_confirmation(post, confirmed):
    with pytest.raises(ValueError, match="Explicit confirmation"):
        confirm_post_availability(post, availability="archived", expected_version=0, confirmed=confirmed)
    post.refresh_from_db()
    assert post.analytics_availability == "unknown"


def test_older_poll_never_overwrites_newer_observation(post):
    now = timezone.now()
    record_post_observation([post.pk], checked_at=now)
    record_post_observation(
        [post.pk],
        checked_at=now - timedelta(seconds=1),
        classification=classify_analytics_error(object_error(), "threads"),
    )
    post.refresh_from_db()
    assert post.analytics_availability == "available"
    assert post.analytics_failure_count == 0


def test_legacy_flag_is_not_a_reconnect_verdict_and_recovers_only_with_appropriate_success(account):
    account.analytics_needs_reconnect = True
    account.save(update_fields=["analytics_needs_reconnect"])
    status = account_analytics_status(account)
    assert status["verification_pending"] and not status["needs_reconnect"]
    # Threads insights requires the one analytics scope; same endpoint success proves it.
    assert record_account_success(account, context="post")
    account.refresh_from_db()
    assert not account.analytics_needs_reconnect
    account.platform = "instagram"
    account.analytics_needs_reconnect = True
    account.save(update_fields=["platform", "analytics_needs_reconnect"])
    assert not record_account_success(account, context="post")
    assert record_account_success(account, context="account")


def test_post_success_never_clears_account_scope_failure(account):
    classification = AnalyticsErrorClassification("account_scope")
    record_account_failure(account, classification, context="account")
    account.refresh_from_db()
    assert not record_account_success(account, context="post")
    account.refresh_from_db()
    assert account.analytics_needs_reconnect


def test_same_pass_partial_success_never_erases_new_auth_failure(account):
    # Both workers began from the same old account generation/evidence.
    old_success_worker = SocialAccount.objects.get(pk=account.pk)
    record_account_failure(account, AnalyticsErrorClassification("account_scope"), context="post")
    assert not record_account_success(old_success_worker, context="post")
    assert not record_account_success(account, context="post")
    account.refresh_from_db()
    assert account.analytics_needs_reconnect


def test_oauth_reconnect_invalidates_old_poll_and_new_object_failure_does_not_reflag(account, post):
    old = SocialAccount.objects.get(pk=account.pk)
    profile = SimpleNamespace(
        platform_id=account.account_platform_id, name="Synthetic", handle="test", avatar_url="", follower_count=0
    )
    with patch("apps.analytics.tasks.backfill_account_analytics"):
        _create_or_update_account(
            workspace_id=account.workspace_id, platform="threads", profile=profile, access_token="new-synthetic-token"
        )
    assert not record_account_failure(old, AnalyticsErrorClassification("account_auth"), context="post")
    account.refresh_from_db()
    with patch("apps.analytics.tasks._analytics_provider_and_token", return_value=(provider(object_error()), "unused")):
        backfill_account_analytics.now(str(account.pk))
    account.refresh_from_db()
    post.refresh_from_db()
    assert not account.analytics_needs_reconnect
    assert post.analytics_availability == "inaccessible"


def test_failed_account_scope_is_visible_but_post_success_does_not_clear_it(account, post):
    account.platform = "instagram"
    account.save(update_fields=["platform"])
    p = provider()
    p.get_account_metrics.side_effect = APIError("missing permission", raw_response={"error": {"code": 200}})
    _sync_account_metrics(account, timezone.now().date(), provider=p, access_token="unused")
    _sync_account_posts(account, p, "unused", [post], timezone.now().date())
    account.refresh_from_db()
    assert account.analytics_needs_reconnect
    assert account.analytics_reconnect_reason == "account_scope"


def test_successful_account_insights_can_recover_legacy_flag(account):
    account.analytics_needs_reconnect = True
    account.save(update_fields=["analytics_needs_reconnect"])
    p = provider()
    p.get_account_metrics.side_effect = None
    p.get_account_metrics.return_value = AccountMetrics(followers=3)
    _sync_account_metrics(account, timezone.now().date(), provider=p, access_token="unused")
    account.refresh_from_db()
    assert not account.analytics_needs_reconnect


def test_failed_post_eta_uses_backoff_not_a_five_minute_poll(post):
    for _ in range(3):
        record_post_observation([post.pk], classification=classify_analytics_error(object_error(), "threads"))
    post.refresh_from_db()
    assert post_freshness(post)[1] >= post.analytics_attempted_at + timedelta(hours=6)
    assert post_analytics_status(post)["error_category"] == "post_inaccessible"


def test_poll_started_before_user_confirmation_cannot_change_any_status(post):
    earlier = timezone.now()
    confirm_post_availability(post, availability="archived", expected_version=0, confirmed=True)
    version = post.analytics_status_version
    record_post_observation(
        [post.pk], checked_at=earlier, classification=classify_analytics_error(object_error(), "threads")
    )
    post.refresh_from_db()
    assert post.analytics_availability == "archived"
    assert post.analytics_status_version == version
    assert post.analytics_error_category == ""


def test_refresh_advances_generation_but_preserves_scope_verdict(account):
    from providers.types import OAuthTokens

    account.oauth_refresh_token = "synthetic-refresh"
    account.save(update_fields=["oauth_refresh_token"])
    record_account_failure(account, AnalyticsErrorClassification("account_scope"), context="account")
    account.refresh_from_db()
    p = Mock()
    p.refresh_token.return_value = OAuthTokens(access_token="synthetic-rotated", expires_in=3600)
    before = account.analytics_auth_updated_at
    account.refresh_oauth_token(p, enqueue_backfill=False)
    account.refresh_from_db()
    assert account.analytics_auth_updated_at != before
    assert account.analytics_reconnect_reason == "account_scope"
    assert account.analytics_needs_reconnect


def test_older_inflight_token_refresh_cannot_overwrite_newer_grant(account):
    from providers.types import OAuthTokens

    account.oauth_refresh_token = "synthetic-refresh"
    account.save(update_fields=["oauth_refresh_token"])
    newer = timezone.now()

    def rotate_while_waiting(_):
        SocialAccount.objects.filter(pk=account.pk).update(
            oauth_access_token="synthetic-new-grant", analytics_auth_updated_at=newer
        )
        return OAuthTokens(access_token="synthetic-old-rotation", expires_in=3600)

    p = Mock()
    p.refresh_token.side_effect = rotate_while_waiting
    assert account.refresh_oauth_token(p, enqueue_backfill=False) == "synthetic-new-grant"
    account.refresh_from_db()
    assert account.analytics_auth_updated_at == newer
    assert account.oauth_access_token == "synthetic-new-grant"


def test_old_health_result_cannot_revert_newer_oauth_grant(account):
    from apps.social_accounts.tasks import check_social_account_health
    from providers.types import AccountProfile

    newer = timezone.now()

    def newer_grant_during_profile(_):
        SocialAccount.objects.filter(pk=account.pk).update(
            oauth_access_token="synthetic-new-grant", analytics_auth_updated_at=newer
        )
        return AccountProfile(platform_id="test-account", name="Old observation")

    p = Mock()
    p.get_profile.side_effect = newer_grant_during_profile
    with patch("providers.get_provider", return_value=p), patch("apps.social_accounts.tasks.retry_failed_subscription"):
        check_social_account_health.now(str(account.pk))
    account.refresh_from_db()
    assert account.analytics_auth_updated_at == newer
    assert account.oauth_access_token == "synthetic-new-grant"
    assert account.account_name == "Synthetic"


def test_health_token_rotation_fences_a_stale_analytics_error(account):
    from apps.social_accounts.tasks import check_social_account_health
    from providers.types import AccountProfile, OAuthTokens

    account.oauth_refresh_token = "synthetic-refresh"
    account.token_expires_at = timezone.now() - timedelta(minutes=1)
    account.save(update_fields=["oauth_refresh_token", "token_expires_at"])
    old = SocialAccount.objects.get(pk=account.pk)
    p = Mock()
    p.refresh_token.return_value = OAuthTokens(access_token="synthetic-refreshed", expires_in=3600)
    p.get_profile.return_value = AccountProfile(platform_id="test-account", name="Synthetic")
    with patch("providers.get_provider", return_value=p), patch("apps.social_accounts.tasks.retry_failed_subscription"):
        check_social_account_health.now(str(account.pk))
    assert not record_account_failure(old, AnalyticsErrorClassification("account_auth"), context="post")
    account.refresh_from_db()
    assert account.analytics_auth_updated_at is not None
    assert not account.analytics_needs_reconnect


@pytest.mark.parametrize("availability", ["archived", "deleted"])
@pytest.mark.parametrize("retry_attempt", [0, 1])
def test_optional_youtube_fetch_excludes_paused_posts(account, post, availability, retry_attempt):
    from apps.analytics.tasks import _sync_youtube_post_analytics

    account.platform = "youtube"
    account.save(update_fields=["platform"])
    confirm_post_availability(post, availability=availability, expected_version=0, confirmed=True)
    p = provider()
    _sync_youtube_post_analytics(account, p, "unused", timezone.now().date(), retry_attempt=retry_attempt)
    p.get_post_analytics.assert_not_called()


@pytest.mark.parametrize("availability", ["archived", "deleted"])
def test_optional_youtube_ignores_result_if_user_paused_in_flight(account, post, availability):
    from apps.analytics.tasks import _sync_youtube_post_analytics

    account.platform = "youtube"
    account.save(update_fields=["platform"])
    p = provider()

    def annotate_during_fetch(*args, **kwargs):
        confirm_post_availability(post, availability=availability, expected_version=0, confirmed=True)
        return {post.platform_post_id: PostMetrics(extra={"watch_time": 7})}

    p.get_post_analytics.side_effect = annotate_during_fetch
    _sync_youtube_post_analytics(account, p, "unused", timezone.now().date())
    assert not PostInsightsSnapshot.objects.filter(platform_post=post).exists()
    post.refresh_from_db()
    assert post.analytics_availability == availability


def test_post_only_response_surfaces_auth_verdict_without_claiming_visibility(account, post):
    from apps.analytics.api_builders import build_post_analytics

    record_account_failure(account, AnalyticsErrorClassification("account_auth"), context="post")
    payload = build_post_analytics(Post.objects.get(pk=post.post_id)).model_dump(mode="json")
    child = payload["platform_posts"][0]
    assert child["account_status"]["needs_reconnect"] is True
    assert child["account_status"]["category"] == "account_auth"
    assert child["analytics_status"]["availability"] == "unknown"


@pytest.mark.parametrize(
    "category,context", [("account_auth", "post"), ("account_scope", "post"), ("account_auth", "account")]
)
def test_account_scope_recovery_boundary_survives_later_failures_and_worker_reload(account, category, context):
    record_account_failure(account, AnalyticsErrorClassification("account_scope"), context="account")
    account.refresh_from_db()
    old_checked_at = account.analytics_reconnect_checked_at
    record_account_failure(account, AnalyticsErrorClassification(category), context=context)
    next_worker = SocialAccount.objects.get(pk=account.pk)
    assert next_worker.analytics_reconnect_reason == "account_scope"
    assert next_worker.analytics_reconnect_context == "account"
    assert next_worker.analytics_reconnect_checked_at > old_checked_at
    assert not record_account_success(next_worker, context="post")
    assert record_account_success(next_worker, context="account")


def test_account_freshness_does_not_promise_poll_while_reconnect_required(account):
    from apps.analytics.freshness import account_freshness

    record_account_failure(account, AnalyticsErrorClassification("account_scope"), context="account")
    account.refresh_from_db()
    assert account_freshness(account) == (None, None)


def test_meta_refresh_code190_flags_even_without_due_posts(account):
    from apps.analytics.tasks import _analytics_provider_and_token

    account.oauth_refresh_token = "synthetic-refresh"
    account.token_expires_at = timezone.now() - timedelta(minutes=1)
    account.save(update_fields=["oauth_refresh_token", "token_expires_at"])
    p = Mock()
    p.refresh_token.side_effect = APIError("not retained", status_code=400, raw_response={"error": {"code": 190}})
    with (
        patch("apps.analytics.tasks._resolve_provider", return_value=p),
        patch("apps.analytics.tasks._enqueue_health_check") as health,
    ):
        _analytics_provider_and_token(account)
    account.refresh_from_db()
    assert account.analytics_needs_reconnect
    assert account.analytics_reconnect_reason == "account_auth"
    health.assert_called_once()


def test_newer_weaker_failure_still_fences_old_account_success(account):
    record_account_failure(account, AnalyticsErrorClassification("account_scope"), context="account")
    old_worker = SocialAccount.objects.get(pk=account.pk)
    record_account_failure(account, AnalyticsErrorClassification("account_auth"), context="post")
    assert not record_account_success(old_worker, context="account")
    account.refresh_from_db()
    assert account.analytics_needs_reconnect
    assert account.analytics_reconnect_reason == "account_scope"
    assert account.analytics_reconnect_context == "account"
