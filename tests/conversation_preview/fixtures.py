"""Invented observations inserted directly, without ingestion or providers."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from django.conf import settings

from apps.accounts.models import User
from apps.inbox.models import (
    ConversationMessage,
    ConversationSyncState,
    ConversationWorkState,
    InboxConversation,
    InboxMessage,
    SendOperation,
)
from apps.members.models import CustomRole, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

NAMESPACE = UUID("617fd8ae-9567-4fdb-9423-a3c73759ebd4")
SCENARIOS = (
    ("burst", "短訊連發", "三則文字與一則分享；保留原始時間及最新回覆目標"),
    ("native", "原生端傳出", "傳出只是觀測；作者、對應問題與處理結果仍未知"),
    ("paused", "回覆流程暫停", "暫停範圍僅限新回覆流程，歷史覆蓋仍為部分"),
    ("unknown", "身分不明", "已撤回對話關聯；不依名稱或時間猜測歸組"),
    ("retracted", "內容已撤回", "保留觀測位置，隱藏撤回文字與附件"),
    ("empty", "沒有觀測", "既有工作項目尚未連結 V2 歷史"),
    ("failed", "觀測失敗", "最近失敗不等於對方沒有回覆"),
    ("bounded", "分頁與截斷", "固定每頁筆數；長文字、附件和更早紀錄都有邊界"),
)
SCENARIO_KEYS = {item[0] for item in SCENARIOS}
DEMO_EMAIL = "preview-only@example.invalid"
BASE_TIME = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)


def synthetic_id(label):
    return uuid5(NAMESPACE, label)


def enrollments():
    return [
        {
            "workspace_id": str(synthetic_id("workspace")),
            "social_account_id": str(synthetic_id("account-" + key)),
            "platform": "facebook" if key == "failed" else "instagram_login",
        }
        for key, _label, _description in SCENARIOS
    ]


def seed_synthetic_graph(password):
    """Caller must establish a new disposable DB; refuses any existing user."""
    if not getattr(settings, "SYNTHETIC_TIMELINE_PREVIEW", False) or User.objects.exists():
        raise RuntimeError("Synthetic seed requires an empty, explicitly isolated preview database")
    user = User(id=synthetic_id("user"), email=DEMO_EMAIL, name="Local synthetic preview")
    user._skip_default_provisioning = True
    user.set_password(password)
    user.save()
    org = Organization.objects.create(id=synthetic_id("org"), name="Synthetic organization")
    workspace = Workspace.objects.create(id=synthetic_id("workspace"), organization=org, name="Synthetic workspace")
    OrgMembership.objects.create(user=user, organization=org)
    role = CustomRole.objects.create(organization=org, name="Synthetic read only", permissions={"use_inbox": True})
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="viewer", custom_role=role)
    for key, _label, _description in SCENARIOS:
        account = SocialAccount.objects.create(
            id=synthetic_id("account-" + key),
            workspace=workspace,
            platform="facebook" if key == "failed" else "instagram_login",
            account_platform_id="synthetic-account-" + key,
            account_name="Synthetic account",
            connection_status="disconnected",
        )
        work = InboxMessage.objects.create(
            id=synthetic_id("work-" + key),
            workspace=workspace,
            social_account=account,
            platform_message_id="synthetic-work-" + key,
            message_type="dm",
            sender_name="匿名對象",
            body="請問週六可以取貨嗎？",
            status="unread",
            received_at=BASE_TIME + timedelta(minutes=4),
        )
        if key == "empty":
            continue
        conversation = None
        if key != "unknown":
            conversation = InboxConversation.objects.create(
                id=synthetic_id("conversation-" + key),
                workspace=workspace,
                social_account=account,
                platform=account.platform,
                identity_kind="platform",
                platform_conversation_id="synthetic-thread-" + key,
                peer_id="synthetic-peer-" + key,
                revision=4,
            )
        specs = [
            {"body": "你好，想詢問一件商品"},
            {"body": "是這個款式"},
            {
                "body": "",
                "attachments": [{"type": "share", "title": "分享的內容", "availability": "unavailable", "url": ""}],
            },
            {"body": work.body, "legacy_message": work},
        ]
        if key == "unknown":
            specs = [{"body": "這則訊息的對話關聯已撤回", "legacy_message": work, "direction": "unknown"}]
        if key == "retracted":
            specs[-1].update(is_deleted=True, body="RETRACTED_SYNTHETIC_BODY_MUST_NOT_RENDER")
        if key == "bounded":
            specs = [{"body": f"較早的合成觀測 {index + 1}"} for index in range(10)] + specs
            specs[-2] = {
                "body": '<script>alert("synthetic")</script>\n' + "長文字只展示有限片段。" * 160,
                "attachments": [
                    {
                        "type": "image",
                        "title": '<img src=x onerror="alert(1)"> 合成附件 ' + str(index + 1),
                        "url": "javascript:alert(1)",
                        "preview_url": "https://example.invalid/never-fetch.png",
                        "availability": "available",
                    }
                    for index in range(5)
                ],
            }
        target = None
        for index, spec in enumerate(specs):
            row = ConversationMessage.objects.create(
                id=synthetic_id(f"message-{key}-{index}"),
                workspace=workspace,
                social_account=account,
                platform=account.platform,
                conversation=conversation,
                conversation_attribution="platform" if conversation else "",
                platform_message_id=f"synthetic-mid-{key}-{index}",
                occurred_at=BASE_TIME + timedelta(minutes=index),
                sources=["webhook"],
                **{"direction": "inbound", **spec},
            )
            ConversationMessage.objects.filter(pk=row.pk).update(
                first_seen_at=BASE_TIME + timedelta(minutes=index, seconds=12)
            )
            if spec.get("legacy_message"):
                target = row
                InboxMessage.objects.filter(pk=work.pk).update(received_at=row.occurred_at)
        if key in {"native", "paused", "failed"}:
            outgoing = ConversationMessage.objects.create(
                id=synthetic_id("outgoing-" + key),
                workspace=workspace,
                social_account=account,
                platform=account.platform,
                conversation=conversation,
                conversation_attribution="platform",
                platform_message_id="synthetic-native-" + key,
                direction="outbound",
                body="這是一則在原生端觀測到的合成回覆",
                occurred_at=BASE_TIME + timedelta(minutes=5),
                sources=["poll"],
            )
            ConversationMessage.objects.filter(pk=outgoing.pk).update(first_seen_at=BASE_TIME + timedelta(minutes=9))
        if key != "unknown":
            ConversationSyncState.objects.create(
                workspace=workspace,
                social_account=account,
                platform=account.platform,
                stream="dm",
                status="failed" if key == "failed" else "success",
                coverage="partial",
                last_attempt_at=BASE_TIME + timedelta(minutes=10),
                last_success_at=BASE_TIME + timedelta(minutes=6),
            )
            state = ConversationWorkState.objects.create(
                conversation=conversation,
                latest_incoming=target,
                generation=2,
                conversation_revision=4,
                owner_paused=key in {"paused", "native", "failed"},
                pause_reason="owner_requested" if key == "paused" else "outgoing_observed" if key == "native" else "",
                history_gap=key == "failed",
                identity_quarantined=key == "retracted",
                due_at=BASE_TIME + timedelta(minutes=4, seconds=20),
            )
            if key == "failed":
                operation = SendOperation.objects.create(
                    workspace=workspace,
                    social_account=account,
                    platform=account.platform,
                    conversation=conversation,
                    actor_scope="synthetic-only",
                    idempotency_key="synthetic-only",
                    payload_fingerprint="0" * 64,
                    body="HIDDEN_SYNTHETIC_DRAFT_MUST_NOT_RENDER",
                    target=target,
                    expected_revision=4,
                    expected_generation=2,
                    status="outcome_unknown",
                )
                state.active_operation = operation
                state.save(update_fields=["active_operation"])
    return user
