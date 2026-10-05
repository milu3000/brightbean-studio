"""Additive rollout preserves old evidence and permits old-slug INSERTs."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.mark.django_db(transaction=True)
def test_analytics_schema_preserves_rows_flags_and_old_model_writes(organization):
    workspace = Workspace.objects.create(organization=organization, name="Synthetic migration")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="threads",
        account_platform_id="existing",
        account_name="Existing",
        analytics_needs_reconnect=True,
    )
    parent = Post.objects.create(workspace=workspace, caption="Existing")
    post = PlatformPost.objects.create(
        post=parent,
        social_account=account,
        platform_post_id="old",
        status="published",
        published_at=timezone.now(),
        analytics_failure_count=2,
    )
    executor = MigrationExecutor(connection)
    heads = executor.loader.graph.leaf_nodes()
    before = [
        ("composer", "0022_platformpost_analytics_column_defaults"),
        ("social_accounts", "0021_flag_pinterest_missing_boards_write"),
    ]
    try:
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        MigrationExecutor(connection).migrate(heads)
        account.refresh_from_db()
        post.refresh_from_db()
        assert account.analytics_needs_reconnect is True
        assert account.analytics_reconnect_reason == ""
        assert account.analytics_reconnect_checked_at is None
        assert post.status == "published"
        assert post.analytics_failure_count == 2
        assert post.analytics_availability == "unknown"
        assert post.analytics_availability_source == ""
        assert post.analytics_status_version == 0
        # The pre-release process knows none of the new columns. DB defaults
        # must allow its INSERTs during the release/migrate overlap.
        old_account = old.get_model("social_accounts", "SocialAccount").objects.create(
            workspace_id=workspace.pk, platform="threads", account_platform_id="old-slug", account_name="Old slug"
        )
        old_post = old.get_model("composer", "PlatformPost").objects.create(
            post_id=parent.pk, social_account_id=old_account.pk, platform_post_id="old-slug-post"
        )
        fresh = SocialAccount.objects.get(pk=old_account.pk)
        inserted = PlatformPost.objects.get(pk=old_post.pk)
        assert not fresh.analytics_needs_reconnect
        assert fresh.analytics_reconnect_evidence == {}
        assert inserted.analytics_availability == "unknown"
        assert inserted.analytics_error_evidence == {}
        assert inserted.analytics_status_version == 0
    finally:
        MigrationExecutor(connection).migrate(heads)
