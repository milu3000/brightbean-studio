"""Real member authorization and a synthetic-only, read-without-writes UI."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.inbox import conversation_read
from apps.inbox.conversation_read import InvalidTimelineCursorError, MemberReadScope, read_work_timeline
from apps.inbox.models import ConversationMessage, ConversationWorkState, InboxConversation, InboxMessage
from apps.members.models import CustomRole, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from tests.conversation_preview.fixtures import (
    DEMO_EMAIL,
    SCENARIOS,
    enrollments,
    seed_synthetic_graph,
    synthetic_id,
)

pytestmark = pytest.mark.django_db
PASSWORD = "DEMO-TEST-ONLY-never-a-real-account"


@pytest.fixture
def graph(settings):
    settings.SYNTHETIC_TIMELINE_PREVIEW = True
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = enrollments()
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = enrollments()
    return seed_synthetic_graph(PASSWORD)


@pytest.fixture
def preview_client(graph, client, settings):
    settings.ROOT_URLCONF = "tests.conversation_preview.urls"
    settings.LOGIN_URL = "/login/"
    settings.LOGIN_REDIRECT_URL = "/"
    settings.SESSION_SAVE_EVERY_REQUEST = False
    settings.MIDDLEWARE = [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "tests.conversation_preview.views.PreviewHeadersMiddleware",
    ]
    templates = deepcopy(settings.TEMPLATES)
    templates[0]["DIRS"] = [Path(__file__).resolve().parents[3] / "tests/conversation_preview/templates"]
    templates[0]["OPTIONS"]["context_processors"] = [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
    ]
    settings.TEMPLATES = templates
    assert client.login(username=DEMO_EMAIL, password=PASSWORD)
    return client


def read(user, key="burst", **kwargs):
    return read_work_timeline(user, synthetic_id("workspace"), synthetic_id("work-" + key), **kwargs)


def test_custom_role_grants_use_inbox_without_reply_permission(graph):
    membership = WorkspaceMembership.objects.get(user=graph)
    assert membership.workspace_role == "viewer"
    assert membership.effective_permissions == {"use_inbox": True}
    result = read(graph)
    assert len(result["messages"]) == 4
    assert result["messages"][0]["latest_target"]
    assert result["messages"][1]["attachments"][0]["availability"] == "原始內容無法取得"


@pytest.mark.parametrize("role", ["owner", "manager", "editor", "viewer", "client", "contributor"])
def test_builtin_roles_follow_existing_use_inbox_policy(graph, role):
    WorkspaceMembership.objects.filter(user=graph).update(custom_role=None, workspace_role=role)
    if role in {"owner", "manager", "editor"}:
        assert read(graph)["messages"]
    else:
        with pytest.raises(PermissionDenied):
            read(graph)


@pytest.mark.parametrize("mutation", ["inactive", "archived", "membership", "permission", "custom_denies_owner"])
def test_current_grants_override_cached_user_or_role(graph, mutation):
    if mutation == "inactive":
        User.objects.filter(pk=graph.pk).update(is_active=False)
    elif mutation == "archived":
        Workspace.objects.filter(pk=synthetic_id("workspace")).update(is_archived=True)
    elif mutation == "membership":
        WorkspaceMembership.objects.filter(user=graph).delete()
    elif mutation == "permission":
        CustomRole.objects.update(permissions={"use_inbox": False})
    else:
        WorkspaceMembership.objects.filter(user=graph).update(workspace_role="owner")
        CustomRole.objects.update(permissions={"use_inbox": False})
    with pytest.raises(PermissionDenied):
        read(graph)


def test_anonymous_performs_no_protected_query(graph):
    with CaptureQueriesContext(connection) as queries, pytest.raises(PermissionDenied):
        read(AnonymousUser())
    assert not queries.captured_queries


@pytest.mark.parametrize(
    "setting,value",
    [
        ("INBOX_CONVERSATION_V2_ENABLED", False),
        ("INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS", []),
        ("INBOX_CONVERSATION_V2_READ_ACCOUNTS", []),
        ("INBOX_CONVERSATION_V2_READ_ACCOUNTS", '{"bad":true}'),
    ],
)
def test_enrollment_is_required_separately_from_membership(graph, settings, setting, value):
    setattr(settings, setting, value)
    with pytest.raises(PermissionDenied):
        read(graph)


@pytest.mark.parametrize(
    "mutation",
    ["permission", "inactive", "account_platform", "account_workspace", "account_native_id", "conversation_identity"],
)
def test_mid_read_change_discards_result_and_protected_query_cannot_follow(graph, mutation):
    original = conversation_read._coordination

    def revoke(scope, conversation, conversations):
        if mutation == "permission":
            CustomRole.objects.update(permissions={"use_inbox": False})
        elif mutation == "inactive":
            User.objects.filter(pk=graph.pk).update(is_active=False)
        elif mutation == "conversation_identity":
            InboxConversation.objects.filter(pk=conversation.pk).update(peer_id="different-synthetic-peer")
        else:
            values = {
                "account_platform": {"platform": "facebook"},
                "account_native_id": {"account_platform_id": "changed"},
            }
            if mutation == "account_workspace":
                other = Workspace.objects.create(
                    organization=Workspace.objects.get(pk=synthetic_id("workspace")).organization,
                    name="Other synthetic workspace",
                )
                values[mutation] = {"workspace_id": other.pk}
            SocialAccount.objects.filter(pk=scope.account.pk).update(**values[mutation])
        assert not ConversationMessage.objects.filter(
            **scope.ledger, conversation_id__in=conversations.values("pk")
        ).exists()
        return original(scope, conversation, conversations)

    with patch.object(conversation_read, "_coordination", side_effect=revoke), pytest.raises(PermissionDenied):
        read(graph)


def test_lazy_scope_denies_membership_replacement(graph):
    scope = MemberReadScope(graph, synthetic_id("workspace"), synthetic_id("work-burst"))
    member = WorkspaceMembership.objects.get(user=graph)
    role = member.custom_role
    member.delete()
    WorkspaceMembership.objects.create(user=graph, workspace_id=synthetic_id("workspace"), custom_role=role)
    assert not InboxMessage.objects.filter(**scope.legacy).exists()
    with pytest.raises(PermissionDenied):
        scope.check()


@pytest.mark.parametrize(
    "foreign", ["target_account", "target_workspace", "target_platform", "conversation", "latest_target", "operation"]
)
def test_corrupt_cross_scope_foreign_keys_do_not_disclose(graph, foreign):
    target = ConversationMessage.objects.get(legacy_message_id=synthetic_id("work-burst"))
    foreign_target = ConversationMessage.objects.get(legacy_message_id=synthetic_id("work-failed"))
    if foreign == "conversation":
        ConversationMessage.objects.filter(pk=target.pk).update(conversation_id=foreign_target.conversation_id)
        with pytest.raises(PermissionDenied):
            read(graph)
    elif foreign in {"latest_target", "operation"}:
        state = ConversationWorkState.objects.get(conversation_id=target.conversation_id)
        if foreign == "latest_target":
            state.latest_incoming = foreign_target
        else:
            state.active_operation = ConversationWorkState.objects.get(
                conversation_id=foreign_target.conversation_id
            ).active_operation
        state.save()
        result = read(graph)
        assert result["coordination"]["label"] == "流程關係無法驗證"
        assert result["coordination"]["latest_id"] is None
    else:
        values = {
            "target_account": {"social_account_id": foreign_target.social_account_id},
            "target_platform": {"platform": "facebook"},
        }
        if foreign == "target_workspace":
            other = Workspace.objects.create(
                organization=target.workspace.organization, name="Other synthetic workspace"
            )
            values[foreign] = {"workspace_id": other.pk}
        ConversationMessage.objects.filter(pk=target.pk).update(**values[foreign])
        assert read(graph)["messages"] == []


@pytest.mark.parametrize("key", ["native", "paused", "failed"])
def test_native_outgoing_is_unknown_author_and_does_not_resolve_work(graph, key):
    result = read(graph, key)
    outgoing = next(item for item in result["messages"] if item["direction"] == "outbound")
    assert outgoing["author"] == "作者未知"
    assert outgoing["sources"] == "poll"
    assert outgoing["delivery"] == "平台觀測紀錄"
    assert result["work"]["status"] == "Unread"
    assert result["coordination"]["label"] == "新回覆流程已暫停"


def test_local_unverified_outgoing_never_claims_provider_observation(graph):
    ConversationMessage.objects.filter(pk=synthetic_id("outgoing-native")).update(
        delivery_status="delivery_unverified", sources=["legacy_backfill"]
    )
    result = read(graph, "native")
    assert result["messages"][0]["delivery"] == "送出結果未驗證"


def test_unknown_identity_only_shows_exact_linked_observation(graph):
    result = read(graph, "unknown")
    assert len(result["messages"]) == 1
    assert result["messages"][0]["direction"] == "unknown"
    assert result["sync"]["coverage"] == "未知"
    assert read(graph, "empty")["messages"] == []


def test_retracted_content_hidden_in_work_and_timeline(graph):
    work = InboxMessage.objects.get(pk=synthetic_id("work-retracted"))
    work.body = "RETRACTED_SYNTHETIC_BODY_MUST_NOT_RENDER"
    work.save(update_fields=["body"])
    result = read(graph, "retracted")
    assert "RETRACTED_SYNTHETIC_BODY" not in str(result)
    InboxMessage.objects.filter(pk=synthetic_id("work-empty")).update(extra={"is_deleted": True})
    assert read(graph, "empty")["work"]["body"] == "此訊息內容已撤回"


@pytest.mark.parametrize("key", ["empty", "burst"])
@pytest.mark.parametrize("extra", [{"is_deleted": True}, {"message": {"is_deleted": True}}])
def test_canonical_legacy_deletion_hides_linked_text_and_truncation(graph, key, extra):
    withdrawn = "WITHDRAWN_PRIVATE_SYNTHETIC_TEXT" * 100
    work_id = synthetic_id("work-" + key)
    InboxMessage.objects.filter(pk=work_id).update(body=withdrawn, extra=extra)
    ConversationMessage.objects.filter(legacy_message_id=work_id).update(body=withdrawn)
    result = read(graph, key)
    assert "WITHDRAWN_PRIVATE_SYNTHETIC_TEXT" not in str(result)
    assert result["work"]["text_truncated"] is False
    if key == "burst":
        target = next(item for item in result["messages"] if item["latest_target"])
        assert target["deleted"] is True
        assert target["text_truncated"] is False
        assert result["coordination"]["latest_deleted"] is True


@pytest.mark.parametrize(
    "title", [{"access_token": "SYNTHETIC_PRIVATE_METADATA"}, ["SYNTHETIC_PRIVATE_METADATA"], 123, None]
)
def test_attachment_title_never_serializes_structured_metadata(graph, title):
    ConversationMessage.objects.filter(legacy_message_id=synthetic_id("work-burst")).update(
        attachments=[{"type": "file", "title": title, "availability": "available"}]
    )
    result = read(graph)
    assert result["messages"][0]["attachments"][0]["title"] == "附件內容"
    assert "SYNTHETIC_PRIVATE_METADATA" not in str(result)


def test_foreign_organization_custom_role_is_not_an_effective_grant(graph):
    foreign = Organization.objects.create(name="Other synthetic organization")
    role = CustomRole.objects.create(organization=foreign, name="Foreign role", permissions={"use_inbox": True})
    WorkspaceMembership.objects.filter(user=graph).update(custom_role=role)
    with pytest.raises(PermissionDenied):
        read(graph)


@pytest.mark.parametrize("move", ["role", "workspace", "both"])
def test_organization_reassignment_revokes_captured_scope_and_cursor(graph, move):
    scope = MemberReadScope(graph, synthetic_id("workspace"), synthetic_id("work-bounded"))
    cursor = read(graph, "bounded")["next_cursor"]
    foreign = Organization.objects.create(name="Other synthetic organization")
    if move in {"role", "both"}:
        CustomRole.objects.update(organization=foreign)
    if move in {"workspace", "both"}:
        Workspace.objects.filter(pk=synthetic_id("workspace")).update(organization=foreign)
    assert not InboxMessage.objects.filter(**scope.legacy).exists()
    with pytest.raises(PermissionDenied):
        scope.check()
    with pytest.raises((PermissionDenied, InvalidTimelineCursorError)):
        read(graph, "bounded", cursor=cursor)


def test_stable_cursor_uses_tie_breaker_and_freezes_new_rows(graph):
    common = timezone.now() - timedelta(hours=1)
    ConversationMessage.objects.filter(conversation_id=synthetic_id("conversation-bounded")).update(
        first_seen_at=common
    )
    first = read(graph, "bounded")
    assert len(first["messages"]) == conversation_read.PAGE_SIZE
    target = ConversationMessage.objects.get(legacy_message_id=synthetic_id("work-bounded"))
    ConversationMessage.objects.create(
        workspace=target.workspace,
        social_account=target.social_account,
        platform=target.platform,
        conversation=target.conversation,
        body="LATER_OBSERVATION_EXCLUDED",
    )
    second = read(graph, "bounded", cursor=first["next_cursor"])
    ids = [item["id"] for item in first["messages"] + second["messages"]]
    assert len(ids) == len(set(ids)) == 14
    assert "LATER_OBSERVATION_EXCLUDED" not in str(second)
    assert second["next_cursor"] is None


@pytest.mark.parametrize(
    "mutation",
    ["role", "new_membership", "new_user", "native_identity", "peer_identity", "bridge", "scenario", "enrollment"],
)
def test_cursor_rejects_changed_identity_or_grants(graph, settings, mutation):
    cursor = read(graph, "bounded")["next_cursor"]
    user = graph
    key = "bounded"
    if mutation == "role":
        CustomRole.objects.update(permissions={"use_inbox": True, "reply_from_inbox": False})
    elif mutation == "new_membership":
        member = WorkspaceMembership.objects.get(user=graph)
        role = member.custom_role
        member.delete()
        WorkspaceMembership.objects.create(user=graph, workspace_id=synthetic_id("workspace"), custom_role=role)
    elif mutation == "new_user":
        user = User.objects.create_user("other-synthetic@example.invalid", PASSWORD)
        WorkspaceMembership.objects.create(user=user, workspace_id=synthetic_id("workspace"), workspace_role="owner")
    elif mutation == "native_identity":
        SocialAccount.objects.filter(pk=synthetic_id("account-bounded")).update(account_platform_id="replacement")
    elif mutation == "peer_identity":
        InboxConversation.objects.filter(pk=synthetic_id("conversation-bounded")).update(peer_id="replacement")
    elif mutation == "bridge":
        ConversationMessage.objects.filter(legacy_message_id=synthetic_id("work-bounded")).update(conversation=None)
    elif mutation == "scenario":
        key = "burst"
    else:
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    with pytest.raises((InvalidTimelineCursorError, PermissionDenied)):
        read(user, key, cursor=cursor)


@pytest.mark.parametrize("cursor", ["", "tampered", "x" * 4097])
def test_invalid_cursor_is_rejected(graph, cursor):
    with pytest.raises(InvalidTimelineCursorError):
        read(graph, "bounded", cursor=cursor)


def test_expired_cursor_is_rejected(graph):
    with patch("django.core.signing.time.time", return_value=1):
        cursor = read(graph, "bounded")["next_cursor"]
    with pytest.raises(InvalidTimelineCursorError):
        read(graph, "bounded", cursor=cursor)


def test_authentication_required_and_actual_password_login(preview_client):
    preview_client.logout()
    assert preview_client.get("/scenario/burst/").status_code == 302
    assert preview_client.get("/login/").status_code == 200
    assert preview_client.post("/login/", {"username": DEMO_EMAIL, "password": "wrong"}).status_code == 200
    assert preview_client.post("/login/", {"username": DEMO_EMAIL, "password": PASSWORD}).status_code == 302
    assert preview_client.get("/scenario/burst/").status_code == 200


@pytest.mark.parametrize("key", [item[0] for item in SCENARIOS])
def test_authenticated_navigation_has_no_database_writes_or_provider_calls(preview_client, graph, key):
    with (
        CaptureQueriesContext(connection) as queries,
        patch("socket.socket.connect", side_effect=AssertionError("No outbound network")),
    ):
        response = preview_client.get(f"/scenario/{key}/")
    assert response.status_code == 200
    assert all(
        query["sql"].lstrip().split()[0].upper() in {"SELECT", "BEGIN", "COMMIT", "SAVEPOINT", "RELEASE"}
        for query in queries.captured_queries
    )
    assert response["Cache-Control"] == "private, no-store"
    assert "default-src 'none'" in response["Content-Security-Policy"]
    graph.refresh_from_db()
    assert graph.last_workspace_id is None
    assert InboxMessage.objects.get(pk=synthetic_id("work-" + key)).status == "unread"
    html = response.content.decode()
    assert "本地合成資料" in html
    assert ("Facebook" if key == "failed" else "Instagram") in html
    assert "instagram_login" not in html
    assert "HIDDEN_SYNTHETIC_DRAFT" not in html
    assert "RETRACTED_SYNTHETIC_BODY" not in html
    assert "<button" not in html and "<form" not in html and "<script" not in html
    assert "<img" not in html and "<iframe" not in html and "<video" not in html
    assert "claim_token" not in html and "idempotency_key" not in html


def test_hostile_body_attachment_and_urls_render_as_text_only(preview_client):
    response = preview_client.get("/scenario/bounded/")
    html = response.content.decode()
    assert "&lt;script&gt;" in html and "&lt;img" in html
    assert "javascript:" not in html and "never-fetch.png" not in html
    assert "文字已截斷" in html and "附件已截斷" in html
    assert "查看更早的本地觀測" in html
    assert len(html) < 32000


def test_malformed_attachment_metadata_is_bounded(graph):
    ConversationMessage.objects.filter(legacy_message_id=synthetic_id("work-burst")).update(
        attachments=[{"type": [], "title": "x" * 3000}, None]
    )
    result = read(graph)
    assert result["messages"][0]["attachments"][0]["kind"] == "unknown"
    assert len(result["messages"][0]["attachments"][0]["title"]) == 160


def test_ui_cursor_error_and_mutations_are_rejected(preview_client):
    response = preview_client.get("/scenario/bounded/?cursor=invalid")
    assert response.status_code == 400 and "分頁已失效" in response.content.decode()
    assert preview_client.post("/scenario/burst/", {"action": "send"}).status_code == 405
    assert preview_client.get("/scenario/unknown-scenario/").status_code == 403
    assert preview_client.post("/send/").status_code == 404
    assert preview_client.post("/pause/").status_code == 404


def test_semantic_navigation_and_self_contained_assets(preview_client):
    html = preview_client.get("/scenario/burst/").content.decode()
    assert 'name="viewport"' in html
    assert 'aria-label="合成情境"' in html
    assert 'href="#timeline"' in html and 'id="timeline"' in html
    for key, _label, _description in SCENARIOS:
        assert f'href="/scenario/{key}/"' in html
    assert "<link" not in html and "url(" not in html
