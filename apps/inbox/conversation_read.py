"""Bounded read adapter for a logged-in workspace member.

Unused by production routes. Unlike the MCP adapter this accepts a real Django
user, not an API-key-shaped surrogate. Human inbox grants are workspace-wide.
Every protected query embeds the current membership and pinned account; a final
check discards the result if those identities changed while assembling it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db.models import F, Q
from django.utils import timezone

from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from providers.meta_inbox_content import is_deleted_content

from .conversation_policy import read_allowed
from .models import (
    ConversationMessage,
    ConversationSyncState,
    ConversationWorkState,
    InboxConversation,
    InboxMessage,
    SendOperation,
)

PAGE_SIZE = 8
TEXT_LIMIT = 1200
ATTACHMENT_LIMIT = 3
CURSOR_SALT = "brightbean.member-timeline.v1"
CURSOR_AGE = 3600


class InvalidTimelineCursorError(ValueError):
    pass


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class MemberReadScope:
    """One captured grant, checked lazily again at each protected SQL query."""

    def __init__(self, user, workspace_id, work_id):
        if not user.is_authenticated:
            raise PermissionDenied
        membership = (
            WorkspaceMembership.objects.select_related("custom_role", "workspace")
            .filter(user_id=user.pk, user__is_active=True, workspace_id=workspace_id, workspace__is_archived=False)
            .filter(Q(custom_role__isnull=True) | Q(custom_role__organization_id=F("workspace__organization_id")))
            .first()
        )
        if (
            membership is None
            or not isinstance(membership.effective_permissions, dict)
            or not membership.effective_permissions.get("use_inbox", False)
        ):
            raise PermissionDenied
        grant = {
            "pk": membership.pk,
            "user_id": user.pk,
            "user__is_active": True,
            "workspace_id": workspace_id,
            "workspace__is_archived": False,
            "workspace__organization_id": membership.workspace.organization_id,
            "workspace_role": membership.workspace_role,
            "custom_role_id": membership.custom_role_id,
        }
        if membership.custom_role_id:
            grant["custom_role__permissions"] = membership.custom_role.permissions
            grant["custom_role__organization_id"] = membership.workspace.organization_id
        self.grants = WorkspaceMembership.objects.filter(**grant).values("workspace_id")
        allowed = SocialAccount.objects.filter(workspace_id=workspace_id, workspace_id__in=self.grants)
        identities = Q(pk__in=[])
        for account in allowed.only("id", "workspace_id", "platform", "account_platform_id"):
            if read_allowed(account):
                identities |= Q(
                    pk=account.pk,
                    workspace_id=account.workspace_id,
                    platform=account.platform,
                    account_platform_id=account.account_platform_id,
                )
        eligible = allowed.filter(identities).values("pk")
        account_id = (
            InboxMessage.objects.filter(
                pk=work_id, workspace_id=workspace_id, social_account_id__in=eligible, message_type="dm"
            )
            .values_list("social_account_id", flat=True)
            .first()
        )
        self.account = (
            allowed.filter(pk=account_id, pk__in=eligible)
            .only("id", "workspace_id", "platform", "account_platform_id")
            .first()
        )
        if self.account is None or not read_allowed(self.account):
            raise PermissionDenied
        self.identity = {
            "pk": self.account.pk,
            "workspace_id": self.account.workspace_id,
            "platform": self.account.platform,
            "account_platform_id": self.account.account_platform_id,
        }
        self.accounts = SocialAccount.objects.filter(**self.identity, workspace_id__in=self.grants).values("pk")
        self.legacy = {
            "workspace_id": self.account.workspace_id,
            "social_account_id__in": self.accounts,
        }
        self.ledger = {**self.legacy, "platform": self.account.platform}
        self.signature = _fingerprint({"grant": grant, "account": self.identity})

    def check(self):
        if not read_allowed(self.account) or not self.accounts.exists():
            raise PermissionDenied


def _message(row, latest_id=None, *, deleted=False):
    """Never expose provider URLs or raw payloads; cap every variable field."""
    attachments = row.attachments if isinstance(row.attachments, list) else []
    cards = []
    deleted = deleted or row.is_deleted
    if not deleted:
        for value in attachments[:ATTACHMENT_LIMIT]:
            item = value if isinstance(value, dict) else {}
            kind = item.get("type")
            title = item.get("title")
            cards.append(
                {
                    "kind": kind
                    if isinstance(kind, str) and kind in {"share", "image", "video", "audio", "file"}
                    else "unknown",
                    "title": title[:160] if isinstance(title, str) and title else "附件內容",
                    "availability": "未載入；網址不代表內容可用"
                    if item.get("availability") == "available"
                    else "原始內容無法取得",
                }
            )
    sources = row.sources if isinstance(row.sources, list) else []
    source_labels = {"poll": "poll", "webhook": "webhook", "app_send": "app_send", "legacy_backfill": "legacy_backfill"}
    direction = row.direction if row.direction in {"inbound", "outbound"} else "unknown"
    body = "" if deleted else row.body or ""
    return {
        "id": str(row.pk),
        "direction": direction,
        "direction_label": {"inbound": "收到", "outbound": "傳出觀測", "unknown": "方向未知"}[direction],
        "author": "作者未知" if direction == "outbound" else "匿名對象" if direction == "inbound" else "身分未知",
        "body": body[:TEXT_LIMIT],
        "text_truncated": len(body) > TEXT_LIMIT,
        "attachments": cards,
        "attachments_truncated": not deleted and len(attachments) > ATTACHMENT_LIMIT,
        "deleted": deleted,
        "occurred_at": row.occurred_at,
        "first_seen_at": row.first_seen_at,
        "sources": ", ".join(source_labels[s] for s in source_labels if s in sources) or "unknown",
        "delivery": {
            "observed": "平台觀測紀錄",
            "provider_accepted": "平台接受紀錄；非送達保證",
            "delivery_unverified": "送出結果未驗證",
        }.get(row.delivery_status, "送出狀態未知"),
        "latest_target": row.pk == latest_id,
    }


def _read_cursor(raw, signature):
    if raw is None:
        return timezone.now(), None
    try:
        if not isinstance(raw, str) or not raw or len(raw) > 4096:
            raise ValueError
        data = signing.loads(raw, salt=CURSOR_SALT, max_age=CURSOR_AGE)
        if not isinstance(data, dict) or data["scope"] != signature:
            raise ValueError
        snapshot = datetime.fromisoformat(data["snapshot"])
        stamp = datetime.fromisoformat(data["stamp"])
        row_id = UUID(data["id"])
        if timezone.is_naive(snapshot) or timezone.is_naive(stamp) or stamp > snapshot:
            raise ValueError
        return snapshot, (stamp, row_id)
    except (signing.BadSignature, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise InvalidTimelineCursorError("分頁已失效或範圍已變更，請回到第一頁") from exc


def _coordination(scope, conversation, conversations):
    result = {"label": "尚無流程狀態", "latest_id": None, "state": None, "operation": "無本地操作觀測"}
    if conversation is None or not getattr(settings, "INBOX_REPLY_COORDINATION_ENABLED", False):
        return result
    states = ConversationWorkState.objects.filter(conversation_id__in=conversations.values("pk"))
    state = states.first()
    operations = SendOperation.objects.filter(**scope.ledger, conversation_id__in=conversations.values("pk"))
    active_ids = list(
        operations.filter(status__in=["prepared", "claimed", "outcome_unknown"]).values_list("pk", flat=True)[:2]
    )
    if state is None:
        if active_ids:
            result["label"] = "流程關係無法驗證"
        return result
    latest = None
    if state.latest_incoming_id:
        latest = ConversationMessage.objects.filter(
            **scope.ledger,
            conversation_id__in=conversations.values("pk"),
            pk=state.latest_incoming_id,
            direction="inbound",
        ).first()
    operation = operations.filter(pk=state.active_operation_id).first() if state.active_operation_id else None
    operation_target = None
    if operation:
        operation_target = ConversationMessage.objects.filter(
            **scope.ledger, conversation_id__in=conversations.values("pk"), pk=operation.target_id, direction="inbound"
        ).first()
    if (
        (state.latest_incoming_id and latest is None)
        or active_ids != ([state.active_operation_id] if state.active_operation_id else [])
        or (state.active_operation_id and (operation is None or operation_target is None))
    ):
        result["label"] = "流程關係無法驗證"
        return result
    result.update(
        label="新回覆流程已暫停" if state.owner_paused else "已記錄的流程狀態",
        latest_id=latest.pk if latest else None,
        latest_deleted=latest.is_deleted if latest else False,
        state={
            "generation": state.generation,
            "revision": state.conversation_revision,
            "revision_matches": state.conversation_revision == conversation.revision,
            "history_gap": state.history_gap,
            "ordering_uncertain": state.ordering_uncertain,
            "identity_quarantined": state.identity_quarantined,
            "due_at": state.due_at,
        },
    )
    if operation:
        result["operation"] = {
            "prepared": "本地準備紀錄",
            "claimed": "本地 dry-run 保留",
            "outcome_unknown": "結果未知，需要人工釐清",
            "confirmed": "本地明確核對紀錄",
            "failed": "明確未送出紀錄",
            "superseded": "已被取代的本地紀錄",
        }.get(operation.status, "未知本地操作狀態")
    return result


def read_work_timeline(user, workspace_id, work_id, *, cursor=None):
    """Read one existing work item and its exactly linked observed context.

    Authenticated membership is re-resolved, never accepted from the browser or
    cached RBAC middleware. New rows are frozen by first_seen_at; edits are not
    frozen. No mark-read, resolve, refresh, provider call or coordination write.
    """
    scope = MemberReadScope(user, workspace_id, work_id)
    work_query = InboxMessage.objects.filter(**scope.legacy, pk=work_id, message_type="dm")
    work = work_query.first()
    if work is None:
        raise PermissionDenied
    target_query = ConversationMessage.objects.filter(**scope.ledger, legacy_message_id=work.pk)
    target = target_query.first()
    conversation = None
    conversations = InboxConversation.objects.none()
    if target and target.conversation_id:
        conversation = InboxConversation.objects.filter(**scope.ledger, pk=target.conversation_id).first()
        if conversation is None:
            raise PermissionDenied
        # Identity fields, not revision: appended observations must not reorder
        # an existing cursor, while reassignment/retraction invalidates it.
        identity = {
            "pk": conversation.pk,
            "identity_kind": conversation.identity_kind,
            "platform_conversation_id": conversation.platform_conversation_id,
            "peer_id": conversation.peer_id,
            "peer_ambiguous": conversation.peer_ambiguous,
        }
        conversations = InboxConversation.objects.filter(**scope.ledger, **identity)
    else:
        identity = None
    bridge = (
        {
            "pk": target.pk,
            "conversation_id": target.conversation_id,
            "conversation_attribution": target.conversation_attribution,
            "is_deleted": target.is_deleted,
        }
        if target
        else None
    )
    signature = _fingerprint({"member_scope": scope.signature, "work": work.pk, "identity": identity, "bridge": bridge})
    snapshot, position = _read_cursor(cursor, signature)
    coordination = _coordination(scope, conversation, conversations)
    if conversation:
        history = ConversationMessage.objects.filter(**scope.ledger, conversation_id__in=conversations.values("pk"))
    else:
        # Unknown identity is not permission to guess a thread from its name or
        # fetch other unassigned messages. Only this exact work's linked row.
        history = target_query.filter(conversation__isnull=True)
    history = history.filter(first_seen_at__lte=snapshot)
    if position:
        stamp, row_id = position
        history = history.filter(Q(first_seen_at__lt=stamp) | Q(first_seen_at=stamp, pk__lt=row_id))
    rows = list(history.order_by("-first_seen_at", "-id")[: PAGE_SIZE + 1])
    page = rows[:PAGE_SIZE]
    next_cursor = None
    if len(rows) > PAGE_SIZE:
        last = page[-1]
        next_cursor = signing.dumps(
            {
                "scope": signature,
                "snapshot": snapshot.isoformat(),
                "stamp": last.first_seen_at.isoformat(),
                "id": str(last.pk),
            },
            salt=CURSOR_SALT,
            compress=True,
        )
    sync = ConversationSyncState.objects.filter(**scope.ledger, stream="dm").first()
    scope.check()
    if not work_query.exists() or (conversation and not conversations.exists()):
        raise PermissionDenied
    if target and not target_query.filter(**bridge).exists():
        raise PermissionDenied
    if target is None and target_query.exists():
        raise InvalidTimelineCursorError("對話關聯已變更，請重新讀取")
    work_deleted = bool(target and target.is_deleted) or is_deleted_content(work.extra)
    if target and work_deleted and coordination["latest_id"] == target.pk:
        coordination["latest_deleted"] = True
    return {
        "work": {
            "id": str(work.pk),
            "body": "此訊息內容已撤回" if work_deleted else work.body[:TEXT_LIMIT],
            "text_truncated": not work_deleted and len(work.body) > TEXT_LIMIT,
            "status": work.get_status_display(),
            "received_at": work.received_at,
        },
        "platform": scope.account.platform,
        "identity_label": "對話身分不明，僅顯示此工作項目的觀測"
        if not conversation
        else "對象身分待確認"
        if conversation.peer_ambiguous
        else "已連結的合成對話",
        "messages": [
            _message(row, coordination["latest_id"], deleted=bool(work_deleted and target and row.pk == target.pk))
            for row in page
        ],
        "coordination": coordination,
        "sync": {
            "status": {
                "unknown": "未知",
                "running": "觀測中",
                "success": "有成功觀測紀錄",
                "failed": "最近觀測失敗",
            }.get(sync.status if sync else "unknown", "未知"),
            "coverage": "部分" if sync and sync.coverage == "partial" else "未知",
            "last_success_at": sync.last_success_at if sync else None,
        },
        "snapshot": snapshot,
        "next_cursor": next_cursor,
        "page_size": PAGE_SIZE,
        "text_limit": TEXT_LIMIT,
        "attachment_limit": ATTACHMENT_LIMIT,
    }
