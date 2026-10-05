"""REST, MCP and browser confirmation boundaries for local annotations."""

import json

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.api_keys.models import ApiKeyAuditLog
from apps.api_keys.services import issue_api_key
from apps.composer.models import PlatformPost, Post
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(user, organization):
    ws = Workspace.objects.create(name="Status mutation", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    membership = WorkspaceMembership.objects.create(user=user, workspace=ws, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=ws, platform="threads", account_platform_id="synthetic", account_name="Synthetic"
    )
    post = Post.objects.create(workspace=ws, caption="Synthetic")
    pp = PlatformPost.objects.create(
        post=post, social_account=account, status="published", platform_post_id="1234", published_at=timezone.now()
    )
    key = issue_api_key(
        workspace=ws, social_accounts=[account], issued_by=user, name="synthetic", permissions=list(PERMISSION_KEYS)
    )
    return ws, membership, account, pp, key


def api(setup, payload):
    pp, key = setup[3:]
    return Client(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}").post(
        f"/api/v1/analytics/platform-posts/{pp.pk}/availability",
        data=json.dumps(payload),
        content_type="application/json",
        secure=True,
    )


def body(**kwargs):
    return {"availability": "archived", "expected_version": 0, "confirmed": True, **kwargs}


def test_rest_annotation_records_source_and_audit_rejects_stale_retry(setup):
    response = api(setup, body())
    assert response.status_code == 200, response.content
    assert response.json()["availability"] == "archived"
    assert response.json()["source"] == "user"
    pp = setup[3]
    pp.refresh_from_db()
    assert pp.status == "published"
    assert ApiKeyAuditLog.objects.filter(action="analytics.status.confirm", target_id=pp.pk).exists()
    assert api(setup, body(availability="deleted")).status_code == 409
    pp.refresh_from_db()
    assert pp.analytics_availability == "archived"


@pytest.mark.parametrize(
    "payload",
    [body(confirmed=False), body(confirmed="yes"), body(expected_version=True), body(availability="available")],
)
def test_rest_requires_exact_confirmation_and_valid_schema(setup, payload):
    assert api(setup, payload).status_code in {409, 422}
    pp = setup[3]
    pp.refresh_from_db()
    assert pp.analytics_availability == "unknown"


def test_rest_denies_out_of_scope_account(setup):
    setup[4].api_key.social_accounts.clear()
    assert api(setup, body()).status_code == 403
    pp = setup[3]
    pp.refresh_from_db()
    assert pp.analytics_availability == "unknown"


def test_rest_denies_read_only_user(setup):
    membership = setup[1]
    membership.workspace_role = "viewer"
    membership.save(update_fields=["workspace_role"])
    assert api(setup, body()).status_code == 403


def test_browser_confirmation_is_csrf_protected_and_never_get_mutation(setup, user):
    ws, _, _, pp, _ = setup
    url = reverse("analytics:confirm_post_status", kwargs={"workspace_id": ws.pk, "post_id": pp.pk})
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.get(url, secure=True).status_code == 405
    assert (
        client.post(
            url, {"availability": "archived", "expected_version": 0, "confirmed": "yes"}, secure=True
        ).status_code
        == 403
    )
    pp.refresh_from_db()
    assert pp.analytics_availability == "unknown"


def test_browser_confirmation_and_stale_error_keep_drawer(setup, user):
    ws, _, _, pp, _ = setup
    url = reverse("analytics:confirm_post_status", kwargs={"workspace_id": ws.pk, "post_id": pp.pk})
    client = Client()
    client.force_login(user)
    payload = {"availability": "archived", "expected_version": 0, "confirmed": "yes"}
    response = client.post(url, payload, secure=True, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert response.headers["HX-Trigger"] == "analyticsStatusChanged"
    stale = client.post(url, {**payload, "availability": "deleted"}, secure=True, HTTP_HX_REQUEST="true")
    assert stale.status_code == 200
    assert "HX-Trigger" not in stale.headers
    assert "status changed" in stale.content.decode()
    pp.refresh_from_db()
    assert pp.analytics_availability == "archived"


def test_mcp_confirmation_uses_same_state_and_rejects_scope(setup):
    from apps.mcp.handlers import _confirm_post_analytics_status
    from apps.mcp.protocol import JsonRpcError

    _, membership, _, pp, key = setup
    context = {"api_key": key.api_key, "membership": membership}
    result = _confirm_post_analytics_status({"platform_post_id": str(pp.pk), **body()}, context)
    decoded = json.loads(result["content"][0]["text"])
    assert decoded["availability"] == "archived" and decoded["version"] == 1
    key.api_key.social_accounts.clear()
    with pytest.raises(JsonRpcError):
        _confirm_post_analytics_status({"platform_post_id": str(pp.pk), **body(expected_version=1)}, context)


def test_mcp_does_not_accept_implicit_or_truthy_confirmation(setup):
    from apps.mcp.handlers import _confirm_post_analytics_status
    from apps.mcp.protocol import JsonRpcError

    _, membership, _, pp, key = setup
    with pytest.raises(JsonRpcError, match="Explicit confirmation"):
        _confirm_post_analytics_status(
            {"platform_post_id": str(pp.pk), **body(confirmed=1)}, {"api_key": key.api_key, "membership": membership}
        )
    pp.refresh_from_db()
    assert pp.analytics_availability == "unknown"
