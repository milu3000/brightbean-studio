"""Conversation V2 is history, never a second work queue or a send guarantee."""

from datetime import timedelta
from io import StringIO
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.inbox.conversations import begin_sync, finish_sync, record_reply, upsert_conversation_message
from apps.inbox.models import ConversationMessage, ConversationSyncState, InboxConversation, InboxMessage, InboxReply
from apps.inbox.services import create_reply_draft, send_reply_now
from apps.inbox.tasks import InboxSyncEngine
from apps.inbox.tests.test_ingestion_events import subscription as event_subscription  # noqa: F401
from apps.inbox.webhooks import _handle_facebook_messaging
from apps.mcp.models import EventOutbox
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("v2")]


@pytest.fixture
def v2(settings):
    settings.INBOX_CONVERSATION_V2_ENABLED = True


def _ingest(account, source, *, mid="native-1", outbound=True, recipient="customer-1", conversation="", deleted=False):
    sender = account.account_platform_id if outbound else "customer-1"
    recipient = recipient if outbound else account.account_platform_id
    now = timezone.now() - timedelta(minutes=1)
    if source == "webhook":
        payload = {
            "sender": {"id": sender},
            "recipient": {"id": recipient} if recipient else {},
            "timestamp": int(now.timestamp() * 1000),
            "message": {"mid": mid, "text": "Native answer" if outbound else "Question", "is_deleted": deleted},
        }
        if conversation:
            payload["conversation_id"] = conversation
        _handle_facebook_messaging(account, payload)
    else:
        InboxSyncEngine()._upsert_message(
            account,
            SimpleNamespace(
                platform_message_id=mid,
                sender_id=sender,
                sender_name="Account" if outbound else "Customer",
                message_type="dm",
                text="Native answer" if outbound else "Question",
                timestamp=now,
                extra={
                    "conversation_id": conversation,
                    "sender_id": sender,
                    "message_recipient_id": recipient,
                    "is_deleted": deleted,
                },
            ),
        )


def _original(account, *, status="archived"):
    return InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id="original",
        message_type="dm",
        sender_name="Customer",
        sender_handle="not-an-identity-handle",
        body="Question",
        status=status,
        extra={"sender_id": "customer-1"},
        received_at=timezone.now() - timedelta(minutes=5),
    )


def _sent(account, *, mid="native-1"):
    return InboxReply.objects.create(
        inbox_message=_original(account),
        body="Native answer",
        platform_reply_id=mid,
        status="sent",
        sent_at=timezone.now(),
    )


@pytest.mark.parametrize("source", ["webhook", "poll"])
def test_flag_off_preserves_legacy_and_creates_no_ledger(settings, inbox_account, source):
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    _ingest(inbox_account, source)
    _ingest(inbox_account, source, mid="incoming", outbound=False)
    assert InboxMessage.objects.count() == 1
    assert not ConversationMessage.objects.exists()
    assert not InboxConversation.objects.exists()
    assert begin_sync(inbox_account) is None
    assert not ConversationSyncState.objects.exists()


@pytest.mark.parametrize("source", ["webhook", "poll"])
@pytest.mark.parametrize("recipient", ["customer-1", ""])
@pytest.mark.usefixtures("event_subscription")
def test_native_outbound_without_inbound_is_preserved_and_silent(inbox_account, source, recipient):
    with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
        _ingest(inbox_account, source, recipient=recipient)
        _ingest(inbox_account, source, recipient=recipient)
    row = ConversationMessage.objects.get()
    assert row.direction == "outbound"
    assert row.delivery_status == "observed"
    assert row.body == "Native answer"
    assert (row.conversation_id is not None) == bool(recipient)
    assert row.legacy_message_id is None
    assert row.legacy_reply_id is None
    assert not InboxMessage.objects.exists()
    assert not InboxReply.objects.exists()
    assert not EventOutbox.objects.exists()
    notify.assert_not_called()


@pytest.mark.parametrize("order", list(permutations(["webhook", "poll", "app_send"])))
@pytest.mark.usefixtures("event_subscription")
def test_all_native_and_app_source_orders_reconcile_one_account_id(inbox_account, order):
    reply = _sent(inbox_account)
    with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
        for source in order:
            if source == "app_send":
                record_reply(reply)
            else:
                _ingest(inbox_account, source, conversation="thread-1" if source == "poll" else "")
    row = ConversationMessage.objects.get()
    assert row.legacy_reply_id == reply.pk
    assert row.sources == ["app_send", "poll", "webhook"]
    assert row.delivery_status == "observed"
    assert row.direction == "outbound"
    assert row.conversation.platform_conversation_id == "thread-1"
    assert InboxConversation.objects.count() == 1
    assert InboxMessage.objects.count() == 1
    assert not EventOutbox.objects.exists()
    notify.assert_not_called()


@pytest.mark.parametrize("order", [("webhook", "poll"), ("poll", "webhook")])
@pytest.mark.usefixtures("event_subscription")
def test_incoming_dual_write_enriches_exact_identity_without_duplicate_work(inbox_account, order):
    with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
        for source in order:
            _ingest(inbox_account, source, outbound=False, conversation="thread-1" if source == "poll" else "")
    row = ConversationMessage.objects.get()
    assert row.direction == "inbound"
    assert row.legacy_message_id == InboxMessage.objects.get().pk
    assert row.conversation.platform_conversation_id == "thread-1"
    assert InboxConversation.objects.count() == 1
    assert EventOutbox.objects.count() == 1
    notify.assert_called_once()


@pytest.mark.parametrize("foreign_workspace", [False, True])
def test_same_provider_id_never_merges_or_hides_another_accounts_inbound(inbox_account, foreign_workspace):
    workspace = inbox_account.workspace
    if foreign_workspace:
        workspace = Workspace.objects.create(name="Other workspace", organization=workspace.organization)
    other = SocialAccount.objects.create(
        workspace=workspace, platform="facebook", account_platform_id="other", account_name="Other"
    )
    _ingest(other, "poll", conversation="same-thread")
    _ingest(inbox_account, "poll", outbound=False, conversation="same-thread")
    assert ConversationMessage.objects.count() == 2
    assert InboxConversation.objects.count() == 2
    assert InboxMessage.objects.get().social_account_id == inbox_account.pk


@pytest.mark.parametrize(
    "extra",
    [
        {"sender_handle": "customer-1"},
        {"sender_name": "customer-1"},
        {"participant_ids": ["page-1", "customer-1", "customer-2"], "message_recipient_id": "customer-1"},
        {"participant_ids": ["stranger-1", "stranger-2"]},
    ],
)
def test_unverified_or_group_identity_never_uses_pair_fallback(inbox_account, extra):
    row = upsert_conversation_message(
        inbox_account, platform_message_id="unknown", sender_id="page-1", extra=extra, occurred_at=timezone.now()
    )
    assert row.conversation_id is None
    assert not InboxConversation.objects.exists()


def test_exact_real_conversation_is_retained_without_recipient(inbox_account):
    row = upsert_conversation_message(
        inbox_account, platform_message_id="unattributed", sender_id="page-1", extra={"conversation_id": "real-thread"}
    )
    assert row.conversation.platform_conversation_id == "real-thread"
    assert row.conversation.peer_id == ""
    assert row.recipient_id == ""


def test_multiple_provider_threads_for_same_peer_make_pair_only_arrival_unassigned(inbox_account):
    for number in [1, 2]:
        _ingest(inbox_account, "poll", mid=f"native-{number}", conversation=f"thread-{number}")
    _ingest(inbox_account, "webhook", mid="native-3")
    assert InboxConversation.objects.count() == 2
    assert ConversationMessage.objects.get(platform_message_id="native-3").conversation_id is None


@pytest.mark.parametrize("first", ["webhook", "poll"])
def test_monotonic_outbound_tombstones_never_resurrect(inbox_account, first):
    _ingest(inbox_account, first, deleted=True)
    _ingest(inbox_account, "poll", deleted=False)
    _ingest(inbox_account, "webhook", deleted=False)
    row = ConversationMessage.objects.get()
    assert row.is_deleted
    assert row.body == ""
    assert row.attachments == []
    assert not InboxMessage.objects.exists()


def test_projection_does_not_store_raw_payloads_or_credential_urls(inbox_account):
    row = upsert_conversation_message(
        inbox_account,
        platform_message_id="safe",
        sender_id="page-1",
        extra={
            "access_token": "secret-never-project",
            "raw": {"private": "raw-provider-value"},
            "attachments": [
                {"type": "image", "payload": {"url": "https://cdn.fbcdn.net/photo?access_token=secret"}},
                {"type": "file", "payload": {"url": "https://example.com/file"}, "raw_secret": "secret"},
            ],
        },
    )
    assert not hasattr(row, "extra")
    assert "secret" not in str(row.attachments)
    assert row.attachments[0]["url"] == ""
    assert row.attachments[1]["url"] == "https://example.com/file"


def test_unchanged_replay_does_not_bump_revision(inbox_account):
    kwargs = dict(
        platform_message_id="m",
        sender_id="page-1",
        sender_name="Page",
        body="Hello",
        extra={"conversation_id": "thread", "message_recipient_id": "peer"},
        occurred_at=timezone.now(),
    )
    row = upsert_conversation_message(inbox_account, **kwargs)
    revision = InboxConversation.objects.get().revision
    assert revision > 0
    for source in ["poll", "webhook", "poll"]:
        upsert_conversation_message(inbox_account, source=source, **kwargs)
    assert InboxConversation.objects.get().revision == revision
    upsert_conversation_message(inbox_account, **{**kwargs, "body": "Edited"})
    assert InboxConversation.objects.get().revision == revision + 1
    assert ConversationMessage.objects.get().first_seen_at == row.first_seen_at


def test_sent_legacy_without_id_is_unverified_and_idempotent(inbox_account):
    reply = _sent(inbox_account, mid="")
    first = record_reply(reply)
    second = record_reply(reply)
    assert first.pk == second.pk
    assert second.platform_message_id is None
    assert second.delivery_status == "delivery_unverified"
    assert second.legacy_reply_id == reply.pk
    assert ConversationMessage.objects.count() == 1


@pytest.mark.parametrize("unsupported", [False, True])
def test_send_path_records_provider_acceptance_or_uncertainty(inbox_account, unsupported):
    reply = create_reply_draft(message=_original(inbox_account), body="App answer")
    with patch(
        "apps.inbox.services._dispatch_to_platform",
        side_effect=NotImplementedError if unsupported else None,
        return_value="sent-id",
    ):
        send_reply_now(reply)
    row = ConversationMessage.objects.get()
    assert row.legacy_reply_id == reply.pk
    assert row.direction == "outbound"
    assert row.delivery_status == ("delivery_unverified" if unsupported else "provider_accepted")
    assert row.platform_message_id == (None if unsupported else "sent-id")


def test_optional_history_failure_cannot_rollback_accepted_reply(inbox_account):
    reply = create_reply_draft(message=_original(inbox_account), body="App answer")
    with (
        patch("apps.inbox.services._dispatch_to_platform", return_value="sent-id"),
        patch("apps.inbox.conversations.record_reply", side_effect=ValueError("projection failed")),
    ):
        send_reply_now(reply)
    reply.refresh_from_db()
    assert reply.status == "sent"
    assert reply.platform_reply_id == "sent-id"


def test_real_observation_enriches_app_time_but_app_never_overwrites_native_time(inbox_account):
    reply = _sent(inbox_account)
    record_reply(reply)
    native_time = timezone.now() - timedelta(minutes=1)
    row = upsert_conversation_message(
        inbox_account, platform_message_id=reply.platform_reply_id, sender_id="page-1", occurred_at=native_time
    )
    record_reply(reply)
    row.refresh_from_db()
    assert row.occurred_at == native_time
    assert row.delivery_status == "observed"


def test_unverified_reply_can_only_reconcile_via_its_own_later_exact_id(inbox_account):
    reply = _sent(inbox_account, mid="")
    local = record_reply(reply)
    _ingest(inbox_account, "webhook")
    assert ConversationMessage.objects.count() == 2  # Never match body/peer/time.
    reply.platform_reply_id = "native-1"
    reply.save(update_fields=["platform_reply_id"])
    row = record_reply(reply)
    assert ConversationMessage.objects.count() == 1
    assert row.legacy_reply_id == reply.pk
    assert row.pk != local.pk
    assert row.delivery_status == "observed"


def test_scope_mismatch_fails_closed(inbox_account):
    stale = SocialAccount.objects.get(pk=inbox_account.pk)
    other_ws = Workspace.objects.create(name="Moved", organization=inbox_account.workspace.organization)
    SocialAccount.objects.filter(pk=inbox_account.pk).update(workspace=other_ws)
    assert upsert_conversation_message(stale, platform_message_id="m", sender_id="page-1") is None
    assert not ConversationMessage.objects.exists()


def test_per_stream_sync_never_reuses_attempt_as_successful_dm_freshness(inbox_account):
    now = timezone.now()
    first = begin_sync(inbox_account, started_at=now - timedelta(hours=1))
    finish_sync(inbox_account, started_at=first, stream_results={"dm": {"status": "success"}}, imported=True)
    dm = ConversationSyncState.objects.get(stream="dm")
    assert dm.last_success_at == first
    assert dm.coverage == "partial"
    second = begin_sync(inbox_account, started_at=now)
    finish_sync(
        inbox_account,
        started_at=second,
        stream_results={
            "dm": {"status": "failed", "error_code": "private provider error"},
            "comment": {"status": "success"},
        },
        imported=True,
    )
    dm.refresh_from_db()
    assert dm.status == "failed"
    assert dm.last_attempt_at == second
    assert dm.last_success_at == first
    assert dm.last_error_code == "provider_error"
    assert ConversationSyncState.objects.get(stream="comment").last_success_at == second


def test_empty_uninstrumented_or_failed_poll_is_not_claimed_fresh(inbox_account):
    started = begin_sync(inbox_account)
    finish_sync(inbox_account, started_at=started, imported=True)
    dm = ConversationSyncState.objects.get(stream="dm")
    assert dm.status == "unknown"
    assert dm.coverage == "unknown"
    assert dm.last_success_at is None


def test_overlapping_old_poll_cannot_overwrite_new_attempt(inbox_account):
    first = begin_sync(inbox_account, started_at=timezone.now() - timedelta(minutes=1))
    second = begin_sync(inbox_account)
    finish_sync(inbox_account, started_at=first, error_code="provider_error")
    assert ConversationSyncState.objects.get(stream="dm").last_attempt_at == second
    assert ConversationSyncState.objects.get(stream="dm").status == "running"


def test_sync_engine_records_mixed_provider_outcome_truthfully(inbox_account):
    from providers.exceptions import APIError
    from providers.facebook import FacebookProvider

    provider = FacebookProvider({"page_id": "page-1"})
    comment = SimpleNamespace(
        platform_message_id="comment",
        sender_id="customer",
        sender_name="Customer",
        message_type="comment",
        text="Comment",
        timestamp=timezone.now(),
        extra={},
    )
    with (
        patch("apps.inbox.tasks.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch.object(provider, "_fetch_direct_messages", side_effect=APIError("failed")),
        patch.object(provider, "_fetch_post_comments", return_value=[comment]),
        patch.object(InboxSyncEngine, "_notify_new_message"),
    ):
        InboxSyncEngine()._sync_account(inbox_account)
    assert ConversationSyncState.objects.get(stream="dm").status == "failed"
    assert ConversationSyncState.objects.get(stream="dm").last_success_at is None
    assert ConversationSyncState.objects.get(stream="comment").status == "success"
    inbox_account.refresh_from_db()
    assert inbox_account.inbox_last_polled_at is not None  # Legacy stamp is only an attempt.


@pytest.mark.usefixtures("event_subscription")
def test_backfill_preview_apply_and_rerun_are_local_and_preserve_archive(inbox_account):
    reply = _sent(inbox_account, mid="")
    before = list(InboxMessage.objects.values())
    output = StringIO()
    with (
        patch("apps.inbox.tasks.get_provider") as provider,
        patch.object(InboxSyncEngine, "_notify_new_message") as notify,
    ):
        call_command("backfill_conversation_history", workspace=str(inbox_account.workspace_id), stdout=output)
        assert not ConversationMessage.objects.exists()
        for _ in range(2):
            call_command(
                "backfill_conversation_history",
                workspace=str(inbox_account.workspace_id),
                apply=True,
                batch_size=1,
                stdout=output,
            )
    assert ConversationMessage.objects.count() == 2
    assert ConversationMessage.objects.get(legacy_reply=reply).delivery_status == "delivery_unverified"
    assert list(InboxMessage.objects.values()) == before
    assert not EventOutbox.objects.exists()
    provider.assert_not_called()
    notify.assert_not_called()
    assert "Preview only" in output.getvalue()


def test_backfill_requires_explicit_scope_and_enabled_flag(settings, inbox_account):
    settings.INBOX_CONVERSATION_V2_ENABLED = False
    with pytest.raises(CommandError):
        call_command("backfill_conversation_history", workspace=str(inbox_account.workspace_id), apply=True)
    with pytest.raises(CommandError):
        call_command("backfill_conversation_history", workspace="invalid")
    with pytest.raises(CommandError):
        call_command("backfill_conversation_history", workspace=str(inbox_account.workspace_id), batch_size=0)


@pytest.mark.parametrize("order", [("webhook", "poll"), ("poll", "webhook")])
def test_exact_message_thread_supersedes_an_earlier_peer_fallback(inbox_account, order):
    _ingest(inbox_account, "poll", mid="first", conversation="thread-a")
    for source in order:
        _ingest(inbox_account, source, mid="second", conversation="thread-b" if source == "poll" else "")
    row = ConversationMessage.objects.get(platform_message_id="second")
    assert row.conversation.platform_conversation_id == "thread-b"
    assert row.conversation_attribution == "platform"


def test_discovering_ambiguity_retracts_prior_pair_only_attribution(inbox_account):
    _ingest(inbox_account, "poll", mid="first", conversation="thread-a")
    _ingest(inbox_account, "webhook", mid="pair-only")
    assert ConversationMessage.objects.get(platform_message_id="pair-only").conversation_id is not None
    _ingest(inbox_account, "poll", mid="second", conversation="thread-b")
    row = ConversationMessage.objects.get(platform_message_id="pair-only")
    assert row.conversation_id is None
    assert row.conversation_attribution == ""
    assert (
        ConversationMessage.objects.get(platform_message_id="first").conversation.platform_conversation_id == "thread-a"
    )


@pytest.mark.parametrize(
    "participants",
    [
        ["page-1", "customer-1", {}],
        ["page-1", "customer-1", "customer-1"],
        ["page-1", "other"],
        None,
        [],
    ],
)
def test_malformed_or_conflicting_participants_cannot_become_a_pair(inbox_account, participants):
    row = upsert_conversation_message(
        inbox_account,
        platform_message_id="m",
        sender_id="page-1",
        extra={
            "participant_ids": participants,
            "message_recipient_id": "customer-1",
        },
    )
    assert row.conversation_id is None


def test_app_projection_does_not_inherit_or_relabel_group_parent(inbox_account):
    reply = _sent(inbox_account)
    original = reply.inbox_message
    original.extra = {
        "sender_id": "customer-1",
        "conversation_id": "group-thread",
        "participant_ids": ["page-1", "customer-1", "customer-2"],
    }
    original.save(update_fields=["extra"])
    native_group = upsert_conversation_message(
        inbox_account, platform_message_id="group-message", sender_id="customer-1", extra=original.extra
    )
    assert native_group.conversation.peer_id == ""
    row = record_reply(reply)
    assert row.conversation_id is None
    native_group.conversation.refresh_from_db()
    assert native_group.conversation.peer_id == ""
    _ingest(inbox_account, "webhook", mid="unrelated")
    unrelated = ConversationMessage.objects.get(platform_message_id="unrelated")
    assert unrelated.conversation_id != native_group.conversation_id


def test_lower_quality_replays_do_not_replace_native_content_or_name(inbox_account):
    kwargs = dict(platform_message_id="m", sender_id="peer", extra={"conversation_id": "thread"})
    upsert_conversation_message(inbox_account, **kwargs, source="poll", body="Current edit", sender_name="Alice")
    revision = InboxConversation.objects.get().revision
    for source in ["webhook", "legacy_backfill", "webhook"]:
        upsert_conversation_message(inbox_account, **kwargs, source=source, body="Older text", sender_name="peer")
    row = ConversationMessage.objects.get()
    assert row.body == "Current edit"
    assert row.sender_name == "Alice"
    assert InboxConversation.objects.get().revision == revision


def test_app_intent_cannot_overwrite_provider_edited_body(inbox_account):
    reply = _sent(inbox_account)
    upsert_conversation_message(
        inbox_account, platform_message_id="native-1", sender_id="page-1", source="poll", body="Native edited text"
    )
    row = record_reply(reply)
    assert row.body == "Native edited text"


def test_native_thread_without_peer_does_not_create_orphan_on_pair_replay(inbox_account):
    upsert_conversation_message(
        inbox_account, platform_message_id="m", sender_id="page-1", extra={"conversation_id": "thread"}
    )
    _ingest(inbox_account, "webhook", mid="m")
    assert InboxConversation.objects.count() == 1
    # Exact same-ID webhook evidence is enough; no further poll is necessary.
    _ingest(inbox_account, "webhook", mid="next")
    assert InboxConversation.objects.count() == 1
    assert ConversationMessage.objects.get(platform_message_id="next").conversation.platform_conversation_id == "thread"


def test_native_peer_enrichment_merges_existing_fallback_safely(inbox_account):
    upsert_conversation_message(
        inbox_account, platform_message_id="first", sender_id="page-1", extra={"conversation_id": "thread"}
    )
    _ingest(inbox_account, "webhook", mid="second")
    _ingest(inbox_account, "webhook", mid="third")
    assert InboxConversation.objects.count() == 2
    _ingest(inbox_account, "poll", mid="second", conversation="thread")
    assert InboxConversation.objects.count() == 1
    assert (
        ConversationMessage.objects.get(platform_message_id="third").conversation.platform_conversation_id == "thread"
    )


def test_reconciliation_bumps_thread_losing_local_unverified_record(inbox_account):
    reply = _sent(inbox_account, mid="")
    local = record_reply(reply)
    previous_thread = local.conversation
    revision = InboxConversation.objects.get(pk=previous_thread.pk).revision
    upsert_conversation_message(
        inbox_account,
        platform_message_id="native-id",
        sender_id="page-1",
        extra={"conversation_id": "separate-real-thread"},
    )
    reply.platform_reply_id = "native-id"
    reply.save(update_fields=["platform_reply_id"])
    record_reply(reply)
    previous_thread.refresh_from_db()
    assert previous_thread.revision > revision
    assert not previous_thread.messages.exists()


def test_late_group_evidence_retracts_peer_inference_and_stays_monotonic(inbox_account):
    _ingest(inbox_account, "poll", mid="native", conversation="group-thread")
    _ingest(inbox_account, "webhook", mid="earlier-pair")
    group = InboxConversation.objects.get()
    assert group.peer_id == "customer-1"
    upsert_conversation_message(
        inbox_account,
        platform_message_id="native",
        sender_id="page-1",
        extra={
            "conversation_id": "group-thread",
            "participant_ids": ["page-1", "customer-1", "customer-2"],
        },
    )
    group.refresh_from_db()
    assert group.peer_id == ""
    assert group.peer_ambiguous
    assert ConversationMessage.objects.get(platform_message_id="earlier-pair").conversation_id is None
    _ingest(inbox_account, "poll", mid="native", conversation="group-thread")
    group.refresh_from_db()
    assert group.peer_id == ""
    assert group.peer_ambiguous
    _ingest(inbox_account, "webhook", mid="later-pair")
    assert ConversationMessage.objects.get(platform_message_id="later-pair").conversation_id != group.pk


@pytest.mark.parametrize("error_kind", ["provider", "unsupported", "generic"])
def test_empty_successful_dm_stream_survives_other_stream_exception(inbox_account, error_kind):
    from providers.exceptions import APIError
    from providers.facebook import FacebookProvider

    provider = FacebookProvider({"page_id": "page-1"})
    with (
        patch("apps.inbox.tasks.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch.object(provider, "_fetch_direct_messages", return_value=[]),
        patch.object(
            provider,
            "_fetch_post_comments",
            side_effect={
                "provider": APIError("failed"),
                "unsupported": NotImplementedError(),
                "generic": ValueError("failed"),
            }[error_kind],
        ),
    ):
        InboxSyncEngine()._sync_account(inbox_account)
    assert ConversationSyncState.objects.get(stream="dm").status == "success"
    assert ConversationSyncState.objects.get(stream="dm").last_success_at is not None
    assert ConversationSyncState.objects.get(stream="comment").status == "failed"


def test_exact_known_message_can_receive_group_proof_without_repeated_thread_id(inbox_account):
    _ingest(inbox_account, "poll", mid="native", conversation="group-thread")
    row = upsert_conversation_message(
        inbox_account,
        platform_message_id="native",
        sender_id="page-1",
        extra={
            "participant_ids": ["page-1", "customer-1", "customer-2"],
        },
    )
    assert row.conversation.peer_ambiguous
    assert row.conversation.peer_id == ""


@pytest.mark.parametrize("source", ["poll", "webhook"])
@pytest.mark.parametrize("outbound", [False, True])
@pytest.mark.parametrize("flag_enabled", [False, True])
def test_stale_platform_ingestion_fails_closed_before_work_or_history(
    settings, inbox_account, source, outbound, flag_enabled
):
    settings.INBOX_CONVERSATION_V2_ENABLED = flag_enabled
    SocialAccount.objects.filter(pk=inbox_account.pk).update(platform="instagram_login")
    with (
        patch.object(InboxSyncEngine, "_notify_new_message") as notify,
        patch("apps.mcp.events.enqueue_inbox_event") as enqueue,
    ):
        _ingest(inbox_account, source, outbound=outbound)
    assert not ConversationMessage.objects.exists()
    assert not InboxConversation.objects.exists()
    assert not InboxMessage.objects.exists()
    notify.assert_not_called()
    enqueue.assert_not_called()


def test_stale_platform_cannot_begin_sync_in_new_namespace(inbox_account):
    SocialAccount.objects.filter(pk=inbox_account.pk).update(platform="instagram_login")
    assert begin_sync(inbox_account) is None
    assert not ConversationSyncState.objects.exists()


def test_stale_platform_cannot_finish_same_timestamp_sync_in_new_namespace(inbox_account):
    started = begin_sync(inbox_account)
    SocialAccount.objects.filter(pk=inbox_account.pk).update(platform="instagram_login")
    fresh = SocialAccount.objects.get(pk=inbox_account.pk)
    assert begin_sync(fresh, started_at=started) == started
    finish_sync(inbox_account, started_at=started, imported=True, stream_results={"dm": {"status": "success"}})
    assert ConversationSyncState.objects.count() == 4
    assert not ConversationSyncState.objects.exclude(status="running").exists()
    assert not ConversationSyncState.objects.exclude(last_success_at=None).exists()


def test_direct_writer_rejects_stale_platform_snapshot(inbox_account):
    SocialAccount.objects.filter(pk=inbox_account.pk).update(platform="instagram_login")
    assert upsert_conversation_message(inbox_account, platform_message_id="m", sender_id="page-1") is None
    assert not ConversationMessage.objects.exists()
