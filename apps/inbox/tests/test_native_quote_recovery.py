"""Synthetic native payload, quote edit/cancel, and final-boundary proofs."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.inbox.conversation_composer import (
    composer_context,
    retire_unattempted_conversation_draft,
    save_conversation_draft,
)
from apps.inbox.dm_send_gate import COVERAGE_VERSION, DMSendGateError, _fingerprint
from apps.inbox.models import ConversationMessage, DMSendControl, InboxConversation, InboxReply
from apps.inbox.tests.test_conversation_composer_recovery import composer as composer_fixture
from apps.inbox.tests.test_conversation_composer_recovery import inputs, send_conversation_reply
from providers.facebook import FacebookProvider
from providers.instagram_login import InstagramLoginProvider

composer = composer_fixture
pytestmark = pytest.mark.django_db(transaction=True)


def quote_row(flow, direction="inbound", **overrides):
    data = dict(
        workspace=flow.account.workspace,
        social_account=flow.account,
        platform=flow.account.platform,
        conversation=flow.conversation,
        conversation_type="direct",
        conversation_attribution="platform",
        platform_message_id="quoted-native-mid",
        direction=direction,
        sender_id=flow.account.account_platform_id if direction == "outbound" else flow.conversation.peer_id,
        recipient_id=flow.conversation.peer_id if direction == "outbound" else flow.account.account_platform_id,
        sender_name="Synthetic sender",
        body="Quoted synthetic message",
        occurred_at=timezone.now() - timedelta(days=2),
        delivery_status="observed",
    )
    data.update(overrides)
    return ConversationMessage.objects.create(**data)


def native_provider(platform):
    cls = FacebookProvider if platform == "facebook" else InstagramLoginProvider
    provider = cls({"client_id": "synthetic-client", "client_secret": "synthetic-secret", "page_id": "page-1"})
    provider._request = MagicMock(
        return_value=MagicMock(json=MagicMock(return_value={"message_id": "sent-native-mid"}))
    )
    return provider


@pytest.mark.parametrize("platform", ["facebook", "instagram_login"])
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_real_meta_adapter_sends_explicit_same_conversation_quote(composer, platform, direction, settings):
    type(composer.account).objects.filter(pk=composer.account.pk).update(platform=platform)
    InboxConversation.objects.filter(pk=composer.conversation.pk).update(platform=platform)
    ConversationMessage.objects.filter(pk=composer.row.pk).update(platform=platform)
    composer.account.refresh_from_db()
    composer.conversation.refresh_from_db()
    composer.row.refresh_from_db()
    identity = {
        "workspace_id": str(composer.account.workspace_id),
        "social_account_id": str(composer.account.pk),
        "platform": platform,
    }
    settings.INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS = settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = [identity]
    quote = quote_row(composer, direction)
    provider = native_provider(platform)
    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
    ):
        reply = send_conversation_reply(**inputs(composer), quote_target_id=str(quote.pk), automated=True)
    assert reply.status == "sent" and reply.quote_target_id == quote.pk
    payload = provider._request.call_args.kwargs["json"]
    assert payload["reply_to"] == {"mid": quote.platform_message_id}
    assert payload["recipient"] == {"id": composer.conversation.peer_id}
    assert "tag" not in payload


@pytest.mark.parametrize("select_then_cancel", [False, True])
def test_default_and_explicit_cancel_omit_provider_reply_to(composer, select_then_cancel):
    params = inputs(composer)
    reply = save_conversation_draft(**params)
    before = _fingerprint(reply, reply.inbox_message)
    if select_then_cancel:
        quote = quote_row(composer)
        reply = save_conversation_draft(**inputs(composer), quote_target_id=str(quote.pk))
        assert _fingerprint(reply, reply.inbox_message) != before
        reply = save_conversation_draft(**inputs(composer), quote_target_id="")
        assert reply.quote_target_id is None and reply.quote_platform_message_id == ""
    provider = native_provider("facebook")
    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
    ):
        send_conversation_reply(**inputs(composer), quote_target_id="")
    assert "reply_to" not in provider._request.call_args.kwargs["json"]


def test_quote_only_stale_clear_is_rejected(composer):
    quote = quote_row(composer)
    stale = inputs(composer)
    save_conversation_draft(**stale, quote_target_id=str(quote.pk))
    with pytest.raises(DMSendGateError, match="another tab"):
        save_conversation_draft(**stale, quote_target_id="")


@pytest.mark.parametrize("hidden", ["deleted", "expired", "unavailable"])
def test_hidden_quote_preview_is_blank_and_can_be_cleared(composer, hidden):
    quote = quote_row(composer)
    save_conversation_draft(**inputs(composer), quote_target_id=str(quote.pk))
    ConversationMessage.objects.filter(pk=quote.pk).update(is_deleted=hidden == "deleted", content_status=hidden)
    context = composer_context(composer.conversation, authorization=composer.authorization)
    assert context["quote_editable"] and context["can_save_draft"]
    assert context["quote_preview"]["unavailable"] and context["quote_preview"]["body"] == ""
    reply = save_conversation_draft(**inputs(composer), quote_target_id="")
    assert reply.quote_target_id is None


def test_clearing_withdrawn_sole_incoming_quote_saves_but_send_remains_held(composer):
    save_conversation_draft(**inputs(composer), quote_target_id=str(composer.row.pk))
    ConversationMessage.objects.filter(pk=composer.row.pk).update(is_deleted=True)
    reply = save_conversation_draft(**inputs(composer), quote_target_id="")
    assert reply.quote_target_id is None
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(DMSendGateError):
        send_conversation_reply(**inputs(composer))
    provider.assert_not_called()


@pytest.mark.parametrize(
    "mutation", ["foreign_thread", "wrong_peer", "unknown", "no_mid", "deleted", "expired", "unavailable"]
)
def test_invalid_quote_has_no_plain_fallback(composer, mutation):
    quote = quote_row(composer)
    if mutation == "foreign_thread":
        other = InboxConversation.objects.create(
            workspace=composer.account.workspace,
            social_account=composer.account,
            platform=composer.account.platform,
            platform_conversation_id="different",
            peer_id="different-peer",
            identity_kind="platform",
            conversation_type="direct",
        )
        quote.conversation = other
    elif mutation == "wrong_peer":
        quote.sender_id = "different-peer"
    elif mutation == "unknown":
        quote.direction = "unknown"
    elif mutation == "no_mid":
        quote.platform_message_id = None
    elif mutation == "deleted":
        quote.is_deleted = True
    else:
        quote.content_status = mutation
    quote.save()
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(DMSendGateError):
        send_conversation_reply(**inputs(composer), quote_target_id=str(quote.pk))
    provider.assert_not_called()
    assert not InboxReply.objects.exists()


def test_fixed_action_quote_cannot_change_after_provider_acceptance(composer):
    quote = quote_row(composer)
    params = inputs(composer)
    provider = native_provider("facebook")
    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
    ):
        sent = send_conversation_reply(**params, quote_target_id=str(quote.pk))
        same = send_conversation_reply(**params, quote_target_id=str(quote.pk))
        with pytest.raises(DMSendGateError, match="frozen action"):
            send_conversation_reply(**params, quote_target_id="")
    assert sent.pk == same.pk and provider._request.call_count == 1


@pytest.mark.parametrize("enrolled", [False, True])
def test_final_boundary_withdrawal_is_known_not_sent_in_both_gates(composer, enrolled):
    quote = quote_row(composer)
    if enrolled:
        DMSendControl.objects.create(
            workspace=composer.account.workspace,
            social_account=composer.account,
            platform=composer.account.platform,
            account_platform_id=composer.account.account_platform_id,
            paused=False,
            coverage_version=COVERAGE_VERSION,
            coverage_from=timezone.now() - timedelta(hours=1),
        )
    provider = native_provider("facebook")

    def credentials(account):
        ConversationMessage.objects.filter(pk=quote.pk).update(is_deleted=True)
        return {}

    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", side_effect=credentials),
        pytest.raises(DMSendGateError),
    ):
        send_conversation_reply(**inputs(composer), quote_target_id=str(quote.pk))
    provider._request.assert_not_called()
    reply = InboxReply.objects.get(conversation=composer.conversation)
    assert reply.status == "failed" and reply.not_sent_verified


def test_same_quote_uuid_cannot_rebind_changed_native_mid(composer):
    quote = quote_row(composer)
    save_conversation_draft(**inputs(composer), quote_target_id=str(quote.pk))
    ConversationMessage.objects.filter(pk=quote.pk).update(platform_message_id="changed-native-mid")
    with pytest.raises(DMSendGateError, match="quote identity changed"):
        save_conversation_draft(**inputs(composer), quote_target_id=str(quote.pk))
    save_conversation_draft(**inputs(composer), quote_target_id="")
    reply = save_conversation_draft(**inputs(composer), quote_target_id=str(quote.pk))
    assert reply.quote_platform_message_id == "changed-native-mid"


def test_explicit_retire_unattempted_draft_preserves_text_nonce_and_allows_successor(composer):
    first = save_conversation_draft(**inputs(composer))
    ConversationMessage.objects.filter(pk=composer.row.pk).update(is_deleted=True)
    context = composer_context(composer.conversation, authorization=composer.authorization)
    assert context["can_retire_draft"]
    retired = retire_unattempted_conversation_draft(
        message=composer.conversation,
        reply_id=first.pk,
        expected_revision=context["composer_revision"],
        scope_token=context["scope_token"],
        authorization=composer.authorization,
    )
    assert retired.retired_at and retired.body == first.body and retired.action_nonce == first.action_nonce
    quote_row(composer, platform_message_id="new-incoming", occurred_at=timezone.now() - timedelta(seconds=1))
    successor = save_conversation_draft(**inputs(composer))
    assert successor.pk != first.pk


def test_unattempted_retire_never_accepts_unknown_or_an_attempt(composer):
    reply = save_conversation_draft(**inputs(composer))
    InboxReply.objects.filter(pk=reply.pk).update(status="unknown", send_generation=1)
    context = composer_context(composer.conversation, authorization=composer.authorization)
    with pytest.raises(DMSendGateError):
        retire_unattempted_conversation_draft(
            message=composer.conversation,
            reply_id=reply.pk,
            expected_revision=context["composer_revision"],
            scope_token=context["scope_token"],
            authorization=composer.authorization,
        )


def test_old_unattempted_draft_uses_new_valid_inbound_window_without_silently_acknowledging_it(composer):
    ConversationMessage.objects.filter(pk=composer.row.pk).update(occurred_at=timezone.now() - timedelta(hours=25))
    old = save_conversation_draft(**inputs(composer))
    newest = quote_row(
        composer, platform_message_id="latest-inbound", occurred_at=timezone.now() - timedelta(seconds=2)
    )
    InboxConversation.objects.filter(pk=composer.conversation.pk).update(incoming_generation=1)
    provider = native_provider("facebook")
    with (
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
    ):
        sent = send_conversation_reply(**inputs(composer), automated=True)
    assert sent.pk == old.pk and sent.inbox_message.platform_message_id == newest.platform_message_id
    assert sent.conversation_incoming_generation == 0
    composer.conversation.refresh_from_db()
    assert composer.conversation.workflow_state == "needs_action"


def test_quote_cannot_extend_expired_automated_window(composer):
    ConversationMessage.objects.filter(pk=composer.row.pk).update(occurred_at=timezone.now() - timedelta(hours=25))
    quote = quote_row(composer, "outbound", occurred_at=timezone.now() - timedelta(seconds=1))
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(ValueError):
        send_conversation_reply(**inputs(composer), automated=True, quote_target_id=str(quote.pk))
    provider.assert_not_called()


def test_populated_native_quote_prevents_destructive_schema_reversal(composer):
    from importlib import import_module
    from types import SimpleNamespace

    from django.apps import apps
    from django.db import connection

    quote = quote_row(composer)
    save_conversation_draft(**inputs(composer), quote_target_id=str(quote.pk))
    guard = import_module("apps.inbox.migrations.0017_native_reply_quote").preserve_quotes
    with pytest.raises(RuntimeError, match="Native quote identities exist"):
        guard(apps, SimpleNamespace(connection=connection))
    save_conversation_draft(**inputs(composer), quote_target_id="")
    guard(apps, SimpleNamespace(connection=connection))


def test_separate_verified_native_thread_for_same_peer_does_not_override_scoped_intent(composer):
    InboxConversation.objects.create(
        workspace=composer.account.workspace,
        social_account=composer.account,
        platform=composer.account.platform,
        identity_kind="platform",
        platform_conversation_id="other-native-thread",
        conversation_type="direct",
        peer_id=composer.conversation.peer_id,
    )
    quote = quote_row(composer)
    reply = save_conversation_draft(**inputs(composer), quote_target_id=str(quote.pk))
    assert reply.platform_conversation_id == composer.conversation.platform_conversation_id
    assert reply.quote_platform_conversation_id == composer.conversation.platform_conversation_id
