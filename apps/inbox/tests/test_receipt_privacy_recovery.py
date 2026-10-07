"""Fresh default-output and preserved-disconnect privacy proofs."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.api.schemas import InboxMessageResponse, InboxReplyResponse
from apps.inbox.account_disconnect import preserve_inbox_disconnect
from apps.inbox.canonical_content import visible_content
from apps.inbox.canonical_send_target import canonical_projection_view
from apps.inbox.conversation_composer import composer_context, save_conversation_draft
from apps.inbox.dm_send_gate import DMSendGateError
from apps.inbox.models import ConversationMessage, InboxArchiveIdentity, InboxMessage, InboxReply
from apps.inbox.receipt_compaction import reply_display_content
from apps.inbox.tests.test_canonical_reads_rebuilt import context as reader_context
from apps.inbox.tests.test_canonical_reads_rebuilt import relocated_receipt as relocation_fixture
from apps.inbox.tests.test_conversation_composer_recovery import composer as composer_fixture
from apps.inbox.tests.test_conversation_composer_recovery import inputs

composer = composer_fixture
context = reader_context
relocated_receipt = relocation_fixture

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("state", ["deleted", "expired"])
@pytest.mark.parametrize("linked", [True, False])
def test_canonical_shadow_redacts_raw_legacy_body_and_media_even_without_fk(composer, state, linked, settings):
    reply = save_conversation_draft(**inputs(composer))
    incoming = reply.inbox_message
    InboxMessage.objects.filter(pk=incoming.pk).update(
        body="Legacy raw copy", extra={"attachments": [{"type": "image", "url": "https://example.invalid/private.jpg"}]}
    )
    ConversationMessage.objects.filter(pk=composer.row.pk).update(
        is_deleted=state == "deleted", content_status=state, legacy_message_id=incoming.pk if linked else None
    )
    settings.INBOX_CANONICAL_READ_ENABLED = False
    projected = canonical_projection_view(incoming)
    assert projected.body == "" and projected.attachments == []
    response = InboxMessageResponse.from_message(incoming)
    assert response.body == "" and response.attachments == []
    assert response.content_status in {"removed", "expired"}
    assert InboxMessage.objects.get(pk=incoming.pk).body == "Legacy raw copy"


def test_sent_receipt_withdrawal_redacts_body_and_error_against_stale_object(composer):
    reply = save_conversation_draft(**inputs(composer))
    InboxReply.objects.filter(pk=reply.pk).update(
        status="sent", platform_reply_id="outgoing-mid", sent_at=timezone.now(), send_error="Private old error"
    )
    reply.refresh_from_db()
    row = ConversationMessage.objects.create(
        workspace=composer.account.workspace,
        social_account=composer.account,
        platform=composer.account.platform,
        conversation=composer.conversation,
        platform_message_id="outgoing-mid",
        direction="outbound",
        sender_id=composer.account.account_platform_id,
        recipient_id=composer.conversation.peer_id,
        body=reply.body,
        legacy_reply=reply,
        occurred_at=reply.sent_at,
    )
    assert reply_display_content(reply)["body"] == reply.body
    ConversationMessage.objects.filter(pk=row.pk).update(is_deleted=True)
    content = reply_display_content(reply)
    assert content["body"] == content["send_error"] == ""
    dto = InboxReplyResponse.from_reply(reply)
    assert dto.body == dto.send_error == ""


def test_compacted_receipt_never_reads_recovery_body(composer):
    from apps.inbox.models import InboxReplyContentRecovery

    reply = save_conversation_draft(**inputs(composer))
    InboxReplyContentRecovery.objects.create(reply=reply, body="Restricted preserved text", reason="expired")
    InboxReply.objects.filter(pk=reply.pk).update(content_compacted_at=timezone.now())
    assert not reply_display_content(reply)["available"]
    assert InboxReplyResponse.from_reply(reply).body == ""


def test_preserved_disconnect_keeps_unknown_and_exact_archive_read_identity(composer):
    reply = save_conversation_draft(**inputs(composer))
    InboxReply.objects.filter(pk=reply.pk).update(status="unknown", send_generation=1)
    before = InboxReply.objects.values().get(pk=reply.pk)
    composer.row.refresh_from_db()
    disconnected = preserve_inbox_disconnect(composer.account)
    assert disconnected.connection_status == "disconnected"
    assert disconnected.oauth_access_token == disconnected.oauth_refresh_token == ""
    assert InboxReply.objects.values().get(pk=reply.pk) == before
    assert InboxArchiveIdentity.objects.filter(social_account=composer.account).count() == 1
    assert visible_content(composer.row)["body"] == composer.row.body
    type(composer.account).objects.filter(pk=composer.account.pk).update(account_platform_id="different-native-account")
    assert not visible_content(composer.row)["available"]


@pytest.mark.parametrize("kind", ["dm", "comment", "mention", "review"])
def test_late_poll_and_webhook_do_not_repopulate_disconnected_account(composer, kind):
    from apps.inbox.tasks import InboxSyncEngine
    from apps.inbox.webhooks import _create_if_new

    preserve_inbox_disconnect(composer.account)
    with patch("apps.inbox.tasks.InboxSyncEngine._notify_new_message") as notify:
        InboxSyncEngine()._upsert_message(
            composer.account,
            SimpleNamespace(
                platform_message_id="late-poll",
                message_type=kind,
                sender_id="synthetic-peer",
                sender_name="Synthetic",
                text="Do not capture",
                extra={},
                timestamp=timezone.now(),
            ),
        )
        _create_if_new(
            account=composer.account,
            platform_message_id="late-webhook",
            message_type=kind,
            sender_name="Synthetic",
            sender_id="synthetic-peer",
            body="Do not capture",
            extra={},
        )
    assert not InboxMessage.objects.exists()
    notify.assert_not_called()


def test_stale_disconnect_cannot_clear_new_connection_credentials(composer):
    type(composer.account).objects.filter(pk=composer.account.pk).update(
        analytics_auth_updated_at=timezone.now() + timedelta(seconds=1)
    )
    with pytest.raises(DMSendGateError):
        preserve_inbox_disconnect(composer.account)
    assert not InboxArchiveIdentity.objects.exists()


def test_adoption_get_is_explicit_private_safe_and_has_no_writes(composer):
    reply = save_conversation_draft(**inputs(composer))
    # Synthetic historical second draft remains recoverable; viewing it never
    # transfers the active slot or edits its content.
    old = InboxReply.objects.create(inbox_message=reply.inbox_message, body="Historical draft")
    composer.conversation.active_reply = None
    composer.conversation.save(update_fields=["active_reply"])
    before = InboxReply.objects.values().get(pk=old.pk)
    context = composer_context(composer.conversation, authorization=composer.authorization, adopt_reply_id=str(old.pk))
    assert context["adopt_reply"].body == "Historical draft"
    assert InboxReply.objects.values().get(pk=old.pk) == before
    composer.conversation.refresh_from_db()
    assert composer.conversation.active_reply_id is None


def test_projection_exclusion_keeps_missing_false_and_null_keys(composer):
    from apps.inbox.canonical_send_target import exclude_transport_projections

    rows = []
    for index, extra in enumerate(
        [{}, {"transport_projection": False}, {"transport_projection": None}, {"transport_projection": True}]
    ):
        rows.append(
            InboxMessage.objects.create(
                workspace=composer.account.workspace,
                social_account=composer.account,
                platform_message_id=f"synthetic-{index}",
                sender_name="Synthetic",
                extra=extra,
                received_at=timezone.now(),
            )
        )
    assert set(exclude_transport_projections(InboxMessage.objects.all()).values_list("pk", flat=True)) == {
        row.pk for row in rows[:3]
    }


@pytest.mark.parametrize("mutation", ["account", "generation", "enrollment"])
def test_local_draft_body_is_withheld_after_scope_change(composer, mutation, settings):
    reply = save_conversation_draft(**inputs(composer))
    if mutation == "account":
        type(composer.account).objects.filter(pk=composer.account.pk).update(account_platform_id="changed-account")
    elif mutation == "generation":
        from uuid import uuid4

        InboxReply.objects.filter(pk=reply.pk).update(connection_generation=uuid4())
    else:
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    assert not reply_display_content(reply)["available"]
    assert InboxReplyResponse.from_reply(reply).body == ""


def test_verified_native_receipt_relocation_is_visible_without_changing_intent(context, relocated_receipt):
    outgoing, reply, _second, _connection = relocated_receipt
    original = reply.conversation_id
    assert reply_display_content(reply)["body"] == outgoing.body
    assert InboxReplyResponse.from_reply(reply).body == outgoing.body
    reply.refresh_from_db()
    assert reply.conversation_id == original


def test_relocated_receipt_withdrawal_still_masks_every_default_output(context, relocated_receipt):
    outgoing, reply, _second, _connection = relocated_receipt
    ConversationMessage.objects.filter(pk=outgoing.pk).update(is_deleted=True)
    assert reply_display_content(reply)["body"] == ""
    assert InboxReplyResponse.from_reply(reply).body == ""


def test_deferred_reply_identity_cannot_adopt_new_scope(composer):
    reply = save_conversation_draft(**inputs(composer))
    deferred = InboxReply.objects.only("pk").get(pk=reply.pk)
    assert not reply_display_content(deferred)["available"]


def test_relocated_receipt_serializer_remains_visible_in_exact_disconnected_archive(context, relocated_receipt):
    outgoing, reply, _second, _connection = relocated_receipt
    preserve_inbox_disconnect(context.account)
    assert reply_display_content(reply)["body"] == outgoing.body
    assert InboxReplyResponse.from_reply(reply).body == outgoing.body
    InboxArchiveIdentity.objects.filter(social_account=context.account).update(account_platform_id="wrong-native")
    assert not reply_display_content(reply)["available"]


def test_archived_relocated_receipt_uses_exact_old_generation_without_mutating_intent(context, relocated_receipt):
    from apps.inbox import canonical_reads as reader
    from apps.inbox.canonical_access import read_connection
    from apps.inbox.canonical_content import native_receipt_relocation_allowed

    outgoing, reply, second, connection = relocated_receipt
    from apps.inbox.models import ConversationObservationState

    # Planned deadlines remain inert while expiry processing is disabled.
    ConversationObservationState.objects.filter(message=outgoing).update(
        expires_at=timezone.now() - timedelta(days=400)
    )
    original_intent = reply.conversation_id
    original_generation = connection.generation
    account = preserve_inbox_disconnect(context.account)
    connection.refresh_from_db()
    assert connection.generation != original_generation
    saved_connection, archive = read_connection(account)
    assert saved_connection.generation == archive.connection_generation == original_generation
    assert native_receipt_relocation_allowed(outgoing, reply)
    assert visible_content(outgoing)["body"] == outgoing.body
    assert reply_display_content(reply)["body"] == outgoing.body
    assert reader.read_conversation(context.scope, second.pk)["messages"][0]["id"] == str(outgoing.pk)
    reply.refresh_from_db()
    assert reply.conversation_id == original_intent and reply.connection_generation == original_generation


@pytest.mark.parametrize(
    "mutation", ["native", "workspace", "account", "platform", "webhook", "generation", "connection", "reconnected"]
)
def test_archived_relocated_receipt_rejects_changed_archive_scope(context, relocated_receipt, mutation):
    from uuid import uuid4

    from apps.inbox.canonical_content import native_receipt_relocation_allowed

    outgoing, reply, _second, _connection = relocated_receipt
    preserve_inbox_disconnect(context.account)
    if mutation == "reconnected":
        type(context.account).objects.filter(pk=context.account.pk).update(connection_status="connected")
    else:
        changes = {
            "native": {"account_platform_id": "different-native"},
            "workspace": {"workspace_id": None},
            "account": {"social_account_id": None},
            "platform": {"platform": "instagram_login"},
            "webhook": {"webhook_target_id": "different-target"},
            "generation": {"connection_generation": uuid4()},
            "connection": {"archived_connection": None},
        }[mutation]
        if mutation == "workspace":
            from apps.workspaces.models import Workspace

            changes["workspace_id"] = Workspace.objects.create(
                name="Other archive scope", organization=context.account.workspace.organization
            ).pk
        if mutation == "account":
            from apps.social_accounts.models import SocialAccount

            changes["social_account_id"] = SocialAccount.objects.create(
                workspace=context.account.workspace,
                platform=context.account.platform,
                account_platform_id="other-archive-account",
            ).pk
        InboxArchiveIdentity.objects.filter(social_account=context.account).update(**changes)
    assert not native_receipt_relocation_allowed(outgoing, reply)
    assert not visible_content(outgoing)["available"]
    assert reply_display_content(reply)["body"] == ""
    assert InboxReplyResponse.from_reply(reply).body == ""


@pytest.mark.parametrize("restriction", ["withdrawn", "expired", "compacted"])
def test_archived_relocated_receipt_keeps_applied_content_restrictions(context, relocated_receipt, restriction):
    from apps.inbox.models import ConversationObservationState

    outgoing, reply, _second, _connection = relocated_receipt
    preserve_inbox_disconnect(context.account)
    if restriction == "withdrawn":
        ConversationObservationState.objects.filter(message=outgoing).update(withdrawn_at=timezone.now())
    elif restriction == "expired":
        ConversationObservationState.objects.filter(message=outgoing).update(expired_at=timezone.now())
    else:
        InboxReply.objects.filter(pk=reply.pk).update(content_compacted_at=timezone.now())
    assert reply_display_content(reply)["body"] == ""
    assert InboxReplyResponse.from_reply(reply).body == ""
