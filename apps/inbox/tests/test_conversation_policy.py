"""Synthetic enrollment gates; these tests never contact a real provider."""

import json
import subprocess
import sys
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.inbox.conversation_policy import (
    capture_allowed,
    provider_capture_allowed,
    provider_options,
    read_allowed,
    read_available,
)
from apps.inbox.conversations import (
    begin_sync,
    finish_sync,
    link_legacy_message,
    record_reply,
    upsert_conversation_message,
)
from apps.inbox.locking import lock_dm_account
from apps.inbox.models import (
    ConversationMessage,
    ConversationSyncState,
    ConversationWorkState,
    InboxConversation,
    InboxMessage,
    SendOperation,
)
from apps.inbox.tasks import InboxSyncEngine, _is_outgoing_dm
from apps.inbox.tests.test_conversation_history import _ingest, _sent
from apps.inbox.tests.test_ingestion_events import subscription as event_subscription  # noqa: F401
from apps.mcp.models import EventOutbox
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("name", ["INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS", "INBOX_CONVERSATION_V2_READ_ACCOUNTS"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("[", id="invalid-json"),
        pytest.param("self_reference", id="self-reference"),
        pytest.param("[" * 20000 + "]" * 20000, id="deep-json"),
    ],
)
def test_malformed_environment_enrollment_does_not_break_settings_startup(name, value):
    # Exercise the real environment -> base settings -> policy path in an
    # isolated process. No .env, credentials, database or providers are loaded.
    script = """
import json
import os
import sys
from unittest.mock import patch
from django.conf import settings
with patch('environ.Env.read_env'):
    from config.settings import base
settings.configure(
    INBOX_CONVERSATION_V2_ENABLED=base.INBOX_CONVERSATION_V2_ENABLED,
    INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS=base.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS,
    INBOX_CONVERSATION_V2_READ_ACCOUNTS=base.INBOX_CONVERSATION_V2_READ_ACCOUNTS,
)
from apps.inbox.conversation_policy import read_available, _enrollments
if os.environ.get('TEST_DEEP_JSON') == '1':
    sys.setrecursionlimit(300)
    raw = next(value for name, value in os.environ.items() if name.startswith('INBOX_CONVERSATION_V2_') and value.startswith('[['))
    try:
        json.loads(raw)
    except RecursionError:
        pass
    else:
        raise AssertionError('The deep JSON regression must reach the recursion failure path')
assert not read_available()
assert not _enrollments('INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS')
assert not _enrollments('INBOX_CONVERSATION_V2_READ_ACCOUNTS')
print('closed')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            "SECRET_KEY": "synthetic-startup-test",
            "INBOX_CONVERSATION_V2_ENABLED": "true",
            name: f"${name}" if value == "self_reference" else value,
            "TEST_DEEP_JSON": "1" if len(value) > 1000 else "0",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "closed"


@pytest.fixture
def enrolled(settings, inbox_account, enroll_conversation_accounts):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    enroll_conversation_accounts(inbox_account)
    return inbox_account


def _entry(account):
    return provider_options(account)["conversation_v2_scope"]


@pytest.mark.parametrize(
    "invalid",
    [None, "", "[", "null", "{}", "true", "*", {}, True, 1, ["*"], [{"all": True}], [{"platform": []}]],
)
def test_empty_or_malformed_capture_fails_closed(settings, enrolled, invalid):
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = invalid
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = [_entry(enrolled)]
    assert not capture_allowed(enrolled)
    assert not read_allowed(enrolled)
    assert not read_available()
    assert not provider_capture_allowed(provider_options(enrolled), platform="facebook")
    assert upsert_conversation_message(enrolled, platform_message_id="no-capture") is None
    assert not ConversationMessage.objects.exists()


@pytest.mark.parametrize(
    "change", [{"workspace_id": "*"}, {"social_account_id": "all"}, {"platform": "instagram"}, {"extra": "value"}]
)
def test_one_malformed_entry_closes_entire_list(settings, enrolled, change):
    entry = _entry(enrolled)
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = json.dumps([entry, {**entry, **change}])
    assert not capture_allowed(enrolled)


@pytest.mark.parametrize("platform", ["instagram", "threads", "youtube", "all", "*"])
def test_unsupported_platform_cannot_be_enrolled(settings, inbox_account, platform):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    inbox_account.platform = platform
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = [
        {
            "workspace_id": str(inbox_account.workspace_id),
            "social_account_id": str(inbox_account.pk),
            "platform": platform,
        }
    ]
    assert not capture_allowed(inbox_account)
    assert not provider_options(inbox_account)


def test_shadow_read_intersection_and_master(settings, enrolled):
    entry = _entry(enrolled)
    assert capture_allowed(enrolled)
    assert not read_allowed(enrolled)
    assert not read_available()
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = json.dumps([entry])
    assert read_allowed(enrolled)
    assert read_available()
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    assert not read_allowed(enrolled)
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = [entry]
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    assert not capture_allowed(enrolled)
    assert not read_allowed(enrolled)


@pytest.mark.parametrize("source", ["poll", "webhook"])
@pytest.mark.usefixtures("event_subscription")
def test_excluded_account_keeps_legacy_inbound_and_outbound_silence(settings, enrolled, source):
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    with patch.object(InboxSyncEngine, "_notify_new_message"):
        _ingest(enrolled, source, mid="outgoing")
        _ingest(enrolled, source, mid="incoming", outbound=False)
    assert not ConversationMessage.objects.exists()
    assert not InboxConversation.objects.exists()
    assert list(InboxMessage.objects.values_list("platform_message_id", flat=True)) == ["incoming"]
    assert EventOutbox.objects.count() == 1


@pytest.mark.parametrize("removed", ["master", "capture"])
def test_all_capture_entrances_stop_and_preserve_stored_rows(settings, enrolled, removed):
    row = upsert_conversation_message(enrolled, platform_message_id="observed", sender_id="page-1")
    started = begin_sync(enrolled)
    reply = _sent(enrolled, mid="accepted")
    before = list(ConversationMessage.objects.values())
    sync_before = list(ConversationSyncState.objects.order_by("pk").values())
    if removed == "master":
        settings.INBOX_CONVERSATION_V2_ENABLED = False
    else:
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    for source in ("poll", "webhook", "legacy_backfill", "app_send"):
        assert upsert_conversation_message(enrolled, platform_message_id="new", source=source) is None
    assert record_reply(reply) is None
    assert begin_sync(enrolled) is None
    finish_sync(enrolled, started_at=started, imported=True, stream_results={"dm": {"status": "success"}})
    link_legacy_message(row, reply.inbox_message)
    assert list(ConversationMessage.objects.values()) == before
    assert list(ConversationSyncState.objects.order_by("pk").values()) == sync_before


@pytest.mark.parametrize("entrance", ["message", "begin", "finish", "reply", "link"])
def test_enrollment_rechecked_after_lock(settings, enrolled, entrance):
    row = upsert_conversation_message(enrolled, platform_message_id="observed", sender_id="page-1")
    started = begin_sync(enrolled)
    reply = _sent(enrolled, mid="accepted")
    before = list(ConversationMessage.objects.values())
    sync_before = list(ConversationSyncState.objects.order_by("pk").values())

    def revoke_then_lock(*args):
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
        return lock_dm_account(*args)

    with patch("apps.inbox.conversations.lock_dm_account", side_effect=revoke_then_lock):
        if entrance == "message":
            assert upsert_conversation_message(enrolled, platform_message_id="new") is None
        elif entrance == "begin":
            assert begin_sync(enrolled) is None
        elif entrance == "finish":
            finish_sync(enrolled, started_at=started, imported=True, stream_results={"dm": {"status": "success"}})
        elif entrance == "reply":
            assert record_reply(reply) is None
        else:
            link_legacy_message(row, reply.inbox_message)
    assert list(ConversationMessage.objects.values()) == before
    assert list(ConversationSyncState.objects.order_by("pk").values()) == sync_before


@pytest.mark.parametrize("change", ["workspace", "platform"])
@pytest.mark.parametrize("entrance", ["message", "begin", "finish", "reply", "link"])
def test_account_identity_change_under_lock_cannot_switch_enrollment(
    enrolled, enroll_conversation_accounts, change, entrance
):
    row = upsert_conversation_message(enrolled, platform_message_id="observed", sender_id="page-1")
    started = begin_sync(enrolled)
    reply = _sent(enrolled, mid="accepted")
    alternate = SocialAccount.objects.get(pk=enrolled.pk)
    if change == "workspace":
        alternate.workspace = Workspace.objects.create(
            name="Moved synthetic workspace", organization=enrolled.workspace.organization
        )
    else:
        alternate.platform = "instagram_login"
    enroll_conversation_accounts(alternate)
    before = list(ConversationMessage.objects.values())
    sync_before = list(ConversationSyncState.objects.order_by("pk").values())

    def move_then_lock(*args):
        SocialAccount.objects.filter(pk=enrolled.pk).update(
            workspace_id=alternate.workspace_id, platform=alternate.platform
        )
        return lock_dm_account(*args)

    with patch("apps.inbox.conversations.lock_dm_account", side_effect=move_then_lock):
        if entrance == "message":
            assert upsert_conversation_message(enrolled, platform_message_id="new") is None
        elif entrance == "begin":
            assert begin_sync(enrolled) is None
        elif entrance == "finish":
            finish_sync(enrolled, started_at=started, imported=True, stream_results={"dm": {"status": "success"}})
        elif entrance == "reply":
            assert record_reply(reply) is None
        else:
            link_legacy_message(row, reply.inbox_message)
    assert list(ConversationMessage.objects.values()) == before
    assert list(ConversationSyncState.objects.order_by("pk").values()) == sync_before


@pytest.mark.parametrize("source", ["poll", "webhook"])
@pytest.mark.parametrize("removed", ["master", "capture"])
@pytest.mark.usefixtures("event_subscription")
def test_known_native_outbound_replay_remains_silent_after_disable(settings, enrolled, source, removed):
    _ingest(enrolled, "poll", mid="native-outbound")
    before = list(ConversationMessage.objects.values())
    if removed == "master":
        settings.INBOX_CONVERSATION_V2_ENABLED = False
    else:
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    # Even a marker-free replay that looks inbound must not become new work.
    with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
        _ingest(enrolled, source, mid="native-outbound", outbound=False)
    assert not InboxMessage.objects.exists()
    assert not EventOutbox.objects.exists()
    assert list(ConversationMessage.objects.values()) == before
    notify.assert_not_called()
    other = SimpleNamespace(
        pk=uuid4(),
        workspace_id=enrolled.workspace_id,
        platform="facebook",
        account_platform_id="other",
        webhook_target_id="",
    )
    assert not _is_outgoing_dm(other, "peer", {}, platform_message_id="native-outbound")
    other.pk = enrolled.pk
    other.workspace_id = uuid4()
    assert not _is_outgoing_dm(other, "peer", {}, platform_message_id="native-outbound")
    other.workspace_id = enrolled.workspace_id
    other.platform = "instagram_login"
    assert not _is_outgoing_dm(other, "peer", {}, platform_message_id="native-outbound")


def test_native_outbound_never_infers_human_sender_role(enrolled):
    row = upsert_conversation_message(
        enrolled, platform_message_id="native", sender_id="page-1", extra={"sender_role": "human"}
    )
    assert row.direction == "outbound"
    assert row.legacy_reply_id is None
    from apps.mcp.conversation_tools import _message

    assert "sender_role" not in _message(row)
    unknown = upsert_conversation_message(enrolled, platform_message_id="unknown", sender_id="")
    assert unknown.direction == "unknown"


def test_local_backfill_requires_account_and_counts_only_current_enrollment(settings, enrolled):
    _sent(enrolled, mid="accepted")
    excluded = SocialAccount.objects.create(
        workspace=enrolled.workspace, platform="facebook", account_platform_id="excluded", account_name="Synthetic"
    )
    _sent(excluded, mid="excluded-reply")
    output = StringIO()
    call_command("backfill_conversation_history", workspace=str(enrolled.workspace_id), stdout=output)
    assert "1 eligible local DMs and 1 eligible sent/local reply records" in output.getvalue()
    assert not ConversationMessage.objects.exists()
    with pytest.raises(CommandError, match="exact --account"):
        call_command("backfill_conversation_history", workspace=str(enrolled.workspace_id), apply=True)
    with pytest.raises(CommandError, match="not enrolled"):
        call_command(
            "backfill_conversation_history", workspace=str(enrolled.workspace_id), account=str(excluded.pk), apply=True
        )
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
    output = StringIO()
    call_command("backfill_conversation_history", workspace=str(enrolled.workspace_id), stdout=output)
    assert "0 eligible local DMs and 0 eligible sent/local reply records" in output.getvalue()


def test_backfill_reports_actual_projection_after_midrun_revocation(settings, enrolled):
    _sent(enrolled, mid="accepted")
    output = StringIO()
    original = upsert_conversation_message

    def revoke_after_first(*args, **kwargs):
        row = original(*args, **kwargs)
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
        return row

    with patch(
        "apps.inbox.management.commands.backfill_conversation_history.upsert_conversation_message",
        side_effect=revoke_after_first,
    ):
        call_command(
            "backfill_conversation_history",
            workspace=str(enrolled.workspace_id),
            account=str(enrolled.pk),
            apply=True,
            stdout=output,
        )
    assert "Projected 1 local DMs and 0 reply records (initially eligible: 1 DMs, 1 replies)" in output.getvalue()
    assert ConversationMessage.objects.count() == 1


@pytest.mark.parametrize("remote_backfill", [False, True])
def test_provider_construction_binds_exact_account_and_revocation_stops_capture(settings, enrolled, remote_backfill):
    provider = Mock()
    provider.quota_units_used = 0
    provider.last_inbox_stream_results = {"dm": {"status": "success"}}
    message = SimpleNamespace(
        platform_message_id="native-outgoing",
        sender_id="page-1",
        sender_name="Account",
        message_type="dm",
        text="Synthetic",
        timestamp=timezone.now(),
        extra={},
    )

    def revoke(**kwargs):
        settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = []
        return [message]

    provider.get_messages.side_effect = revoke
    target = (
        "apps.inbox.management.commands.backfill_inbox.get_provider"
        if remote_backfill
        else "apps.inbox.tasks.get_provider"
    )
    with (
        patch(target, return_value=provider) as factory,
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch.object(InboxSyncEngine, "_notify_new_message") as notify,
    ):
        if remote_backfill:
            call_command("backfill_inbox", account_id=str(enrolled.pk), stdout=StringIO())
        else:
            InboxSyncEngine()._sync_account(enrolled)
    assert factory.call_args.args[1]["conversation_v2_scope"] == _entry(enrolled)
    assert not ConversationMessage.objects.exists()
    assert not InboxMessage.objects.exists()
    assert not ConversationSyncState.objects.exclude(status="running").exists()
    assert not ConversationSyncState.objects.exclude(last_success_at=None).exists()
    notify.assert_not_called()


@pytest.mark.parametrize("platform", ["facebook", "instagram_login"])
@pytest.mark.usefixtures("event_subscription")
def test_remote_backfill_marks_history_without_scheduling_live_coordination(
    settings, enrolled, enroll_conversation_accounts, platform
):
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    enrolled.platform = platform
    enrolled.save(update_fields=["platform"])
    enroll_conversation_accounts(enrolled)
    provider = Mock()
    provider.get_messages.return_value = [
        SimpleNamespace(
            platform_message_id="historical-incoming",
            sender_id="synthetic-peer",
            sender_name="Synthetic peer",
            message_type="dm",
            text="Synthetic historical question",
            timestamp=timezone.now() - timedelta(days=20),
            extra={"conversation_id": "history-thread", "message_recipient_id": enrolled.account_platform_id},
        ),
        SimpleNamespace(
            platform_message_id="historical-outgoing",
            sender_id=enrolled.account_platform_id,
            sender_name="Synthetic account",
            message_type="dm",
            text="Synthetic historical answer",
            timestamp=timezone.now() - timedelta(days=19),
            extra={"conversation_id": "history-thread", "message_recipient_id": "synthetic-peer"},
        ),
    ]
    with (
        patch("apps.inbox.management.commands.backfill_inbox.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch.object(InboxSyncEngine, "_notify_new_message") as notify,
    ):
        call_command("backfill_inbox", account_id=str(enrolled.pk), stdout=StringIO())
    assert ConversationMessage.objects.count() == 2
    assert list(ConversationMessage.objects.values_list("sources", flat=True)) == [
        ["legacy_backfill"],
        ["legacy_backfill"],
    ]
    assert ConversationMessage.objects.get(platform_message_id="historical-incoming").legacy_message_id
    assert not ConversationMessage.objects.get(platform_message_id="historical-outgoing").legacy_message_id
    assert list(InboxMessage.objects.values_list("platform_message_id", flat=True)) == ["historical-incoming"]
    assert not ConversationWorkState.objects.exists()
    assert not SendOperation.objects.exists()
    assert not EventOutbox.objects.exists()
    notify.assert_not_called()


def test_silent_ordinary_poll_remains_live_observation(settings, enrolled):
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    message = SimpleNamespace(
        platform_message_id="silent-live-poll",
        sender_id="synthetic-peer",
        sender_name="Synthetic peer",
        message_type="dm",
        text="Synthetic current question",
        timestamp=timezone.now(),
        extra={
            "message_recipient_id": enrolled.account_platform_id,
            "participant_ids": [enrolled.account_platform_id, "synthetic-peer"],
        },
    )
    InboxSyncEngine()._upsert_message(enrolled, message, notify=False)
    assert ConversationMessage.objects.get().sources == ["poll"]
    assert ConversationWorkState.objects.get().due_at is not None
    assert not EventOutbox.objects.exists()


def test_poll_source_rejects_unrecognized_internal_provenance(enrolled):
    with pytest.raises(ValueError, match="Unsupported inbox poll source"):
        InboxSyncEngine()._upsert_message(enrolled, None, notify=False, source="app_send")
    assert not ConversationMessage.objects.exists()
    assert not InboxMessage.objects.exists()
