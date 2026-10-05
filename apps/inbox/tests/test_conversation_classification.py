"""Direction endpoints never prove a private thread; uncertainty is retained."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.inbox import reply_coordination as coordinator
from apps.inbox import reply_dispatch as dispatch
from apps.inbox.conversations import upsert_conversation_message
from apps.inbox.dm_send_gate import DMSendGateError
from apps.inbox.models import ConversationMessage, ConversationWorkState, InboxConversation, InboxMessage, InboxReply
from apps.inbox.tests.test_dispatch_ownership import clock as clock  # noqa: F401
from apps.inbox.tests.test_dispatch_ownership import identity  # noqa: F401
from apps.inbox.tests.test_dispatch_ownership import owned as owned
from apps.inbox.tests.test_reply_coordination import claim, observe, prepare
from providers.meta_inbox_content import (
    classify_conversation_identity,
    merge_message_extra,
    polled_conversation_classification,
    polled_message_extra,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def classification_flags(settings, inbox_account, enroll_conversation_accounts):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    settings.INBOX_REPLY_COORDINATION_ENABLED = True
    enroll_conversation_accounts(inbox_account, read=True)


def classify(extra, sender="peer"):
    return classify_conversation_identity(extra, own_ids=["owner"], sender_id=sender)


@pytest.mark.parametrize(
    "extra,kind,reason",
    [
        ({}, "unknown", "participants_missing"),
        ({"recipient": {"id": "owner"}}, "unknown", "participants_missing"),
        ({"participant_ids": ["owner", "peer"]}, "direct", "participants_pair"),
        ({"participants": {"data": [{"id": "owner"}, {"id": "peer"}]}}, "direct", "participants_pair"),
        ({"participant_ids": ["owner", "peer", "other"]}, "group", "participants_group"),
        ({"participant_ids": ["owner", "peer", False]}, "unknown", "participants_invalid"),
        ({"participant_ids": ["owner", "peer", "peer"]}, "unknown", "participants_invalid"),
        ({"participant_ids": ["owner", "peer", {}]}, "unknown", "participants_invalid"),
        ({"participant_ids": ["owner", "peer", ""]}, "unknown", "participants_invalid"),
        ({"participant_ids": None}, "unknown", "participants_invalid"),
        ({"participant_ids": []}, "unknown", "participants_invalid"),
        ({"participant_ids": ["other-owner", "peer"]}, "unknown", "participant_endpoints_conflict"),
        ({"participant_ids": ["owner", "other-peer"]}, "unknown", "participant_endpoints_conflict"),
        (
            {"participant_ids": {"data": ["owner", "peer"], "paging": {"next": "more"}}},
            "unknown",
            "participants_incomplete",
        ),
        (
            {"participant_ids": {"data": ["owner", "peer"], "summary": {"total_count": 3}}},
            "unknown",
            "participants_incomplete",
        ),
        (
            {"participant_ids": ["owner", "peer"], "message_recipient_id": "stranger"},
            "unknown",
            "participant_endpoints_conflict",
        ),
        (
            {"participant_ids": ["owner", "peer"], "message_recipient_id": False},
            "unknown",
            "participant_endpoints_conflict",
        ),
        (
            {"participant_ids": ["owner", "peer"], "sender": {"id": "stranger"}},
            "unknown",
            "participant_endpoints_conflict",
        ),
        (
            {"participant_ids": ["owner", "peer"], "message_recipient_id": "owner", "recipient": {"id": "stranger"}},
            "unknown",
            "participant_endpoints_conflict",
        ),
    ],
)
def test_complete_evidence_matrix(extra, kind, reason):
    actual_kind, actual_reason, peer = classify(extra)
    assert (actual_kind, actual_reason) == (kind, reason)
    assert peer == ("peer" if kind == "direct" else "")


def test_polled_summary_retains_no_new_participant_ids_or_raw_metadata():
    summary = polled_conversation_classification(
        {"to": {"data": [{"id": "owner", "private": "no-retention"}]}},
        own_id="owner",
        sender_id="peer",
        participant_ids={"data": [{"id": "owner"}, {"id": "peer", "private": "no-retention"}, {"id": "other"}]},
    )
    extra = polled_message_extra({}, conversation_id="native", sender_id="peer", classification_summary=summary)
    assert summary == {"conversation_type": "group", "classification_reason": "participants_group"}
    assert "participant_ids" not in extra
    assert "private" not in str(extra)


def test_endpoints_only_same_sender_messages_are_retained_without_pair_merging(inbox_account):
    rows = [
        upsert_conversation_message(
            inbox_account,
            platform_message_id=f"group-member-{index}",
            sender_id="peer",
            extra={"sender": {"id": "peer"}, "recipient": {"id": inbox_account.account_platform_id}},
            source="webhook",
            occurred_at=timezone.now(),
        )
        for index in range(2)
    ]
    assert all(row.direction == "inbound" and row.conversation_type == "unknown" for row in rows)
    assert all(row.conversation_id is None for row in rows)
    assert ConversationMessage.objects.count() == 2
    assert not InboxConversation.objects.exists()
    assert not ConversationWorkState.objects.exists()


@pytest.mark.parametrize("kind,participants", [("group", ["page-1", "peer", "other"]), ("unknown", None)])
def test_native_threads_never_merge_by_shared_sender(inbox_account, kind, participants):
    rows = [
        upsert_conversation_message(
            inbox_account,
            platform_message_id=f"message-{index}",
            sender_id="peer",
            extra={"conversation_id": f"thread-{index}", "participant_ids": participants},
            source="poll",
            occurred_at=timezone.now(),
        )
        for index in range(2)
    ]
    assert rows[0].conversation_id != rows[1].conversation_id
    assert all(row.conversation.conversation_type == kind for row in rows)
    assert all(not row.conversation.peer_id for row in rows)
    assert not ConversationWorkState.objects.exists()


@pytest.mark.parametrize(
    "late",
    [
        {"participant_ids": ["page-1", "peer-1", "other"]},
        {"participant_ids": ["page-1", "peer-1", {}]},
        {"participant_ids": {"data": ["page-1", "peer-1"], "paging": {"next": "more"}}},
    ],
)
@pytest.mark.parametrize("coordination_enabled", [True, False])
def test_late_group_or_invalid_evidence_fences_claims_even_when_coordination_off(
    settings, inbox_account, late, coordination_enabled
):
    actor = coordinator.ReplyActorScope(
        "user:synthetic-operator", inbox_account.workspace_id, frozenset({inbox_account.pk}), True
    )
    row = observe(inbox_account, extra={"conversation_id": "native"})
    operation = claim(actor, prepare(actor, row))
    settings.INBOX_REPLY_COORDINATION_ENABLED = coordination_enabled
    updated = upsert_conversation_message(
        inbox_account, platform_message_id=row.platform_message_id, sender_id="peer-1", extra=late, source="poll"
    )
    operation.refresh_from_db()
    state = ConversationWorkState.objects.get()
    assert updated.conversation.conversation_type != "direct"
    assert updated.conversation.peer_id == ""
    assert operation.status == "superseded"
    assert state.owner_paused and state.due_at is None
    # A later endpoint-only/text-only replay cannot restore sending.
    updated = upsert_conversation_message(
        inbox_account,
        platform_message_id=row.platform_message_id,
        sender_id="peer-1",
        extra={"message_recipient_id": "page-1"},
        source="poll",
    )
    assert updated.conversation.conversation_type != "direct"
    assert not coordinator._verified(updated.conversation)


def test_same_native_message_with_conflicting_thread_id_is_held(inbox_account):
    original = observe(inbox_account, extra={"conversation_id": "thread-a"})
    updated = observe(inbox_account, extra={"conversation_id": "thread-b"})
    assert updated.pk == original.pk
    assert updated.conversation_id == original.conversation_id
    assert updated.conversation.conversation_type == "unknown"
    assert updated.conversation.classification_reason == "identity_conflict"
    assert InboxConversation.objects.count() == 1


@pytest.mark.parametrize(
    "later",
    [
        {},
        {"participant_ids": ["owner", "peer"]},
        {"conversation_type": "direct", "classification_reason": "participants_pair"},
    ],
)
def test_group_evidence_survives_metadata_merges(later):
    original = {
        "participant_ids": ["owner", "peer", "other"],
        "sender_id": "peer",
        "conversation_type": "group",
        "classification_reason": "participants_group",
    }
    merged = merge_message_extra(original, later)
    assert classify(merged)[:2] == ("group", "participants_group")
    summary_only = merge_message_extra(
        {key: original[key] for key in ("conversation_type", "classification_reason")}, later
    )
    assert summary_only["conversation_type"] == "group"
    assert classify(summary_only)[:2] == ("group", "participants_group")


def test_conflicting_complete_pairs_remain_unknown_on_replay():
    first = {
        "participant_ids": ["owner", "peer"],
        "sender_id": "peer",
        "conversation_type": "direct",
        "classification_reason": "participants_pair",
    }
    conflict = merge_message_extra(first, {"participant_ids": ["owner", "other"]})
    assert classify(conflict)[:2] == ("unknown", "identity_conflict")
    assert classify(merge_message_extra(conflict, first))[:2] == ("unknown", "identity_conflict")


def test_legacy_display_does_not_call_endpoint_only_messages_direct(inbox_account):
    message = InboxMessage(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        message_type="dm",
        extra={"sender_id": "peer", "recipient": {"id": "page-1"}},
    )
    assert message.conversation_type == "unknown"
    assert message.classification_reason == "participants_missing"
    assert message.type_display == "Message (type unknown)"
    message.extra = {"conversation_type": "group", "classification_reason": "participants_group"}
    assert message.conversation_type == "group"
    assert message.type_display == "Group Message"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["unknown", "group"])
def test_owned_account_legacy_fallback_blocks_non_direct_other_threads(owned, kind):
    extra = {"sender_id": "different-peer", "conversation_id": "other-thread"}
    if kind == "group":
        extra["participant_ids"] = [owned.account.account_platform_id, "different-peer", "third"]
    message = InboxMessage.objects.create(
        workspace=owned.account.workspace,
        social_account=owned.account,
        platform_message_id="other-message",
        message_type="dm",
        sender_name="Synthetic",
        extra=extra,
        received_at=owned.clock.now,
    )
    row = upsert_conversation_message(
        owned.account,
        platform_message_id=message.platform_message_id,
        sender_id="different-peer",
        extra=extra,
        legacy_message=message,
        occurred_at=owned.clock.now,
    )
    reply = InboxReply.objects.create(inbox_message=message, body="Synthetic draft")
    assert row.conversation.conversation_type == kind
    with pytest.raises(DMSendGateError, match="current owner's V2"):
        dispatch.check_conversation_send(owned.account, message, reply)


@pytest.mark.django_db(transaction=True)
def test_existing_rows_migrate_unknown_without_fabricating_evidence(inbox_account, restore_migrations):
    previous = [("inbox", "0008_dmsendattempt_operation_sendoperation_attempt_and_more")]
    executor = MigrationExecutor(connection)
    heads = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(previous)
        old = executor.loader.project_state(previous).apps
        old_conversation = old.get_model("inbox", "InboxConversation").objects.create(
            workspace_id=inbox_account.workspace_id,
            social_account_id=inbox_account.pk,
            platform=inbox_account.platform,
            peer_id="prior-inferred-peer",
            identity_kind="verified_peer",
        )
        old_message = old.get_model("inbox", "ConversationMessage").objects.create(
            workspace_id=inbox_account.workspace_id,
            social_account_id=inbox_account.pk,
            platform=inbox_account.platform,
            conversation=old_conversation,
            conversation_attribution="verified_peer",
            platform_message_id="prior-inferred-message",
            direction="inbound",
            sender_id="prior-inferred-peer",
            recipient_id=inbox_account.account_platform_id,
            body="Prior text",
        )
        conversation_before = old.get_model("inbox", "InboxConversation").objects.values().get(pk=old_conversation.pk)
        message_before = old.get_model("inbox", "ConversationMessage").objects.values().get(pk=old_message.pk)
        MigrationExecutor(connection).migrate(heads)
        conversation = InboxConversation.objects.get(pk=old_conversation.pk)
        message = ConversationMessage.objects.get(pk=old_message.pk)
        assert conversation.conversation_type == message.conversation_type == "unknown"
        assert conversation.classification_reason == message.classification_reason == "participants_missing"
        assert InboxConversation.objects.values(*conversation_before).get(pk=old_conversation.pk) == conversation_before
        assert ConversationMessage.objects.values(*message_before).get(pk=old_message.pk) == message_before
        assert not coordinator._verified(conversation)
    finally:
        MigrationExecutor(connection).migrate(heads)


def test_same_message_cannot_switch_fallback_peer(inbox_account):
    original = observe(inbox_account)
    updated = observe(inbox_account, peer="different-peer")
    assert updated.pk == original.pk
    assert updated.conversation_type == "unknown"
    assert updated.classification_reason == "identity_conflict"
    assert updated.conversation_id is None
    assert InboxConversation.objects.count() == 1
    assert not coordinator._verified(InboxConversation.objects.get())


def test_conflicting_participant_projections_are_unknown():
    assert classify({"participant_ids": ["owner", "peer"], "participants": {"data": ["owner", "peer", "other"]}})[
        :2
    ] == ("unknown", "identity_conflict")


@pytest.mark.parametrize(
    "recipients,expected",
    [(["owner", "other"], "group"), (["owner", "stranger"], "unknown"), (["peer", "owner"], "unknown")],
)
def test_group_recipient_edge_does_not_imply_a_direct_message(recipients, expected):
    extra = polled_message_extra(
        {"to": {"data": [{"id": value} for value in recipients]}},
        conversation_id="group-native",
        own_id="owner",
        sender_id="peer",
        participant_ids=["owner", "peer", "other"],
    )
    assert extra["conversation_type"] == expected
    assert "message_recipient_id" not in extra


@pytest.mark.parametrize(
    "metadata",
    [
        {"paging": {"previous": "previous-page"}},
        {"truncated": True},
        {"is_truncated": True},
        {"summary": {"total_count": True}},
    ],
)
def test_tail_or_truncated_participant_pair_is_never_direct(metadata):
    assert classify({"participants": {"data": ["owner", "peer"], **metadata}})[0] == "unknown"


@pytest.mark.parametrize("reason", [[], {}, True])
def test_malformed_classification_marker_does_not_crash_or_create_direct_proof(reason):
    assert classify({"classification_reason": reason})[0] == "unknown"


@pytest.mark.django_db(transaction=True)
def test_classification_reverse_refuses_existing_ownership(owned, restore_migrations):
    with pytest.raises(RuntimeError, match="persisted ownership"):
        MigrationExecutor(connection).migrate(
            [("inbox", "0008_dmsendattempt_operation_sendoperation_attempt_and_more")]
        )
    assert InboxConversation.objects.get(pk=owned.row.conversation_id).conversation_type == "direct"
