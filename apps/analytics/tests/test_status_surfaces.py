"""Analytics availability is distinct from publish state on UI, REST and MCP."""

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.analytics.api_builders import _build_platform_post_analytics, build_account_analytics, build_post_analytics
from apps.analytics.models import PostInsightsSnapshot
from apps.analytics.services import all_posts_for, post_detail
from apps.api_keys.services import issue_api_key
from apps.composer.models import PlatformPost, Post
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def status_case(user, organization, client):
    workspace = Workspace.objects.create(name="Status surfaces", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="status-fixture",
        account_name="Status test account",
        connection_status="connected",
    )
    post = Post.objects.create(workspace=workspace, caption="A published post remains published")
    child = PlatformPost.objects.create(
        post=post,
        social_account=account,
        status="published",
        platform_post_id="status-post",
        published_at=timezone.now() - timedelta(days=2),
    )
    client.force_login(user)
    return workspace, account, post, child, membership, client


def _status(child, *, availability="inaccessible", category="post_inaccessible", source="platform"):
    child.analytics_availability = availability
    child.analytics_availability_source = source
    child.analytics_availability_checked_at = timezone.now()
    child.analytics_attempted_at = timezone.now()
    child.analytics_error_category = category
    child.analytics_error_evidence = {"http_status": 400, "code": 100, "subcode": 33}
    child.analytics_status_version = 7
    child.save()


def _detail_url(workspace, child):
    return reverse("analytics:post_detail", kwargs={"workspace_id": workspace.pk, "post_id": child.pk})


def _account_url(workspace, account):
    return reverse("analytics:account", kwargs={"workspace_id": workspace.pk, "account_id": account.pk})


def _saved_snapshot(child, value=37):
    captured = timezone.now() - timedelta(days=1)
    snapshot = PostInsightsSnapshot.objects.create(
        platform_post=child, metric_key="views", date=captured.date(), value=value
    )
    PostInsightsSnapshot.objects.filter(pk=snapshot.pk).update(captured_at=captured)
    return captured


@pytest.mark.parametrize("platform,published", [("instagram", True), ("instagram", False), ("bluesky", True)])
def test_status_present_on_every_post_response_branch(status_case, platform, published):
    _, account, post, child, _, _ = status_case
    account.platform = platform
    account.save(update_fields=["platform"])
    if not published:
        child.status = "draft"
        child.published_at = None
    _status(child)

    body = build_post_analytics(post).model_dump(mode="json")["platform_posts"][0]

    assert body["status"] == ("published" if published else "draft")
    assert body["analytics_status"]["availability"] == "inaccessible"
    assert body["analytics_status"]["error_category"] == "post_inaccessible"
    assert body["analytics_status"]["error_evidence"] == {"http_status": 400, "code": 100, "subcode": 33}
    assert body["analytics_status"]["checked_at"].endswith("Z")
    assert body["analytics_status"]["attempted_at"].endswith("Z")
    assert body["analytics_status"]["version"] == 7
    assert body["metric_tiles"] == []


@pytest.mark.parametrize("platform", ["instagram", "bluesky"])
def test_legacy_account_warning_is_pending_in_all_response_branches(status_case, platform):
    _, account, _, _, _, _ = status_case
    account.platform = platform
    account.analytics_needs_reconnect = True

    body = build_account_analytics(account, 7).model_dump(mode="json")

    assert body["analytics_status"]["needs_reconnect"] is False
    assert body["analytics_status"]["verification_pending"] is True
    assert body["analytics_status"]["evidence"] == {}


def test_legacy_warning_has_no_reconnect_cta_and_is_not_cleared_on_read(status_case):
    workspace, account, _, _, _, client = status_case
    account.analytics_needs_reconnect = True
    account.save(update_fields=["analytics_needs_reconnect"])

    response = client.get(_account_url(workspace, account))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Analytics warning awaiting verification" in body
    assert "Reconnect for analytics" not in body
    account.refresh_from_db()
    assert account.analytics_needs_reconnect is True


@pytest.mark.parametrize("category", ["account_auth", "account_scope"])
def test_reliable_account_failure_has_reconnect_cta_and_check_time(status_case, category):
    workspace, account, _, _, _, client = status_case
    account.analytics_needs_reconnect = True
    account.analytics_reconnect_reason = category
    account.analytics_reconnect_context = "account"
    account.analytics_reconnect_checked_at = timezone.now()
    account.analytics_reconnect_evidence = {"signal": "test"}
    account.save()

    response = client.get(_account_url(workspace, account))

    assert response.status_code == 200
    assert "Reconnect for analytics" in response.content.decode()
    assert "Checked <time" in response.content.decode()
    assert response.context["analytics_status"]["category"] == category


@pytest.mark.parametrize(
    "availability,category,source,label",
    [
        ("archived", "", "user", "Archived"),
        ("deleted", "post_deleted", "platform", "Deleted"),
        ("inaccessible", "post_inaccessible", "platform", "archive or deletion not confirmed"),
        ("unknown", "unknown", "", "Metrics fetch failed; cause unverified"),
        ("available", "transient", "platform", "Temporarily unable to refresh metrics"),
    ],
)
def test_table_and_drawer_show_honest_status_and_saved_time(status_case, availability, category, source, label):
    workspace, account, _, child, _, client = status_case
    captured = _saved_snapshot(child)
    _status(child, availability=availability, category=category, source=source)

    drawer = client.get(_detail_url(workspace, child))
    table = client.get(_account_url(workspace, account), {"partial": "table"}, HTTP_HX_REQUEST="true")

    for response in (drawer, table):
        assert response.status_code == 200
        body = response.content.decode()
        assert label in body
        assert "Latest saved snapshot" in body
        assert captured.date().isoformat() in body
        assert f'data-analytics-availability="{availability}"' in body
        if source == "user":
            assert "User-confirmed" in body
    if category:
        assert "The latest refresh did not provide current metrics." in drawer.content.decode()
    child.refresh_from_db()
    assert child.status == "published"
    assert child.published_at is not None


def test_unknown_without_snapshots_does_not_fabricate_zero_tiles(status_case):
    workspace, account, _, child, _, client = status_case

    detail = post_detail(child)
    table = all_posts_for(account, days_filter=None, sort_key="date")
    body = client.get(_detail_url(workspace, child)).content.decode()
    table_body = client.get(
        _account_url(workspace, account), {"partial": "table"}, HTTP_HX_REQUEST="true"
    ).content.decode()

    assert detail["metric_tiles"] == []
    assert detail["captured_at"] is None
    assert table["rows"][0]["stats"] == {}
    assert table["rows"][0]["analytics_status"]["availability"] == "unknown"
    assert "No saved metrics yet. Missing values are not zero." in body
    assert 'aria-label="No saved value"' in table_body


def test_snapshot_zero_is_preserved_and_missing_metrics_are_omitted(status_case):
    _, _, _, child, _, _ = status_case
    _saved_snapshot(child, value=0)

    detail = post_detail(child)

    assert len(detail["metric_tiles"]) == 1
    assert detail["metric_tiles"][0]["key"] == "views"
    assert detail["metric_tiles"][0]["value"] == 0


@pytest.mark.parametrize("platform,published", [("bluesky", True), ("instagram", False)])
def test_short_circuit_status_does_not_fetch_snapshots(status_case, django_assert_num_queries, platform, published):
    _, account, _, child, _, _ = status_case
    account.platform = platform
    child.social_account = account
    if not published:
        child.published_at = None
    with django_assert_num_queries(0):
        body = _build_platform_post_analytics(child, ["instagram"])
    assert body.analytics_status.availability == "unknown"


def test_confirmation_form_requires_explicit_selection_and_checkbox(status_case):
    workspace, _, _, child, _, client = status_case
    _status(child)

    response = client.get(_detail_url(workspace, child))
    body = response.content.decode()

    assert response.context["can_confirm_status"] is True
    assert 'name="expected_version" value="7"' in body
    assert 'name="availability" required' in body
    assert 'name="confirmed" value="yes" required' in body
    assert "Confirm local annotation" in body
    assert "does not archive or delete the remote post" in body


def test_read_only_member_cannot_see_confirmation_form(status_case):
    workspace, _, _, child, membership, client = status_case
    membership.workspace_role = "viewer"
    membership.save(update_fields=["workspace_role"])

    response = client.get(_detail_url(workspace, child))

    assert response.status_code == 200
    assert response.context["can_confirm_status"] is False
    assert "Confirm local annotation" not in response.content.decode()


@pytest.mark.parametrize(
    "availability,category", [("archived", ""), ("inaccessible", "post_inaccessible"), ("unknown", "transient")]
)
def test_rest_and_mcp_return_identical_post_status(status_case, user, availability, category):
    workspace, account, post, child, _, _ = status_case
    _status(child, availability=availability, category=category)
    _saved_snapshot(child)
    key = issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=user,
        name="status-parity",
        permissions=list(PERMISSION_KEYS),
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
    rest = client.get(f"/api/v1/analytics/posts/{post.pk}", secure=True)
    mcp = client.post(
        "/api/v1/mcp/",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_post_analytics", "arguments": {"post_id": str(post.pk)}},
            }
        ),
        content_type="application/json",
        secure=True,
    )

    assert rest.status_code == 200, rest.content
    assert mcp.status_code == 200, mcp.content
    envelope = mcp.json()
    assert "error" not in envelope, envelope
    assert json.loads(envelope["result"]["content"][0]["text"]) == rest.json()
    assert rest.json()["platform_posts"][0]["analytics_status"]["availability"] == availability
