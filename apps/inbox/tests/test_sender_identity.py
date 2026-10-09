"""Sender labels must never fabricate handles or borrow another user's name."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from django.template.loader import render_to_string
from django.utils import timezone

from apps.inbox import canonical_reads as reader
from apps.inbox.durable_sync import run_one_page
from apps.inbox.meta_sync_adapter import MetaSyncAdapter
from apps.inbox.models import ConversationMessage, InboxConversation, InboxMessage
from apps.inbox.sender_display import normalize_sender_name, sender_display
from apps.inbox.sync_ingestion import ingest_meta_webhook
from apps.inbox.tests.test_canonical_reads_rebuilt import context as _context
from apps.inbox.tests.test_canonical_reads_rebuilt import proof, row
from apps.inbox.tests.test_durable_pages_recovery import checkpoint
from apps.inbox.tests.test_durable_pages_recovery import durable as _durable
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

context = _context
durable = _durable


@pytest.mark.parametrize(
    "sender,expected",
    [
        ({"id": "123456", "name": "Ada Chen", "username": "ada.chen"}, "Ada Chen"),
        ({"id": "123456", "username": "Ada.Chen"}, "Ada.Chen"),
        ({"id": "123456", "name": "  ", "username": "ada.chen"}, "ada.chen"),
        ({"id": "123456", "name": "123456", "username": "ada.chen"}, "ada.chen"),
        ({"id": "123456", "name": "Unknown", "username": "ada.chen"}, "ada.chen"),
        ({"id": "123456", "name": "陳小明"}, "陳小明"),
        ({"id": "123456"}, ""),
        ({"id": "123456", "name": "123456", "username": "123456"}, ""),
        ({"id": "123456", "name": "@123456"}, ""),
        ({"id": "peer-id", "name": "peer-id", "username": "peer-id"}, ""),
        ({"name": {"name": "Do not stringify"}, "username": ["nor this"]}, ""),
        ({"name": "Unsafe\x00name", "username": "line\nusername"}, ""),
        ({"name": "x" * 256, "username": "x" * 256}, ""),
        ({"username": "https://example.com/ada"}, ""),
        ([], ""),
        (None, ""),
    ],
)
def test_name_normalizer_uses_only_well_formed_supplied_name_or_username(sender, expected):
    assert normalize_sender_name(sender) == expected


@pytest.mark.parametrize("platform", ["facebook", "instagram", "instagram_login"])
@pytest.mark.parametrize("native_id", ["123456789012345", "igsid-opaque-id"])
def test_meta_scoped_ids_are_not_names_or_handles(platform, native_id):
    record = {"sender_name": native_id, "sender_handle": native_id, "platform": platform}
    before = dict(record)
    assert sender_display(record) == {
        "label": "Unknown sender",
        "name": "",
        "handle": "",
        "native_id": native_id,
    }
    assert record == before


def test_explicit_username_is_available_without_changing_native_id():
    display = sender_display(
        {"sender_id": "123456", "sender_username": "ada.chen", "sender_handle": "ada.chen"},
        platform="instagram_login",
    )
    assert display == {"label": "@ada.chen", "name": "", "handle": "ada.chen", "native_id": "123456"}


def test_unknown_platform_still_cannot_turn_numeric_handle_into_username():
    assert sender_display({"sender_handle": "123456"})["label"] == "Unknown sender"
    assert sender_display({"sender_handle": "@123456"})["handle"] == ""
    assert (
        sender_display({"sender_name": "Ada", "sender_handle": "ada.chen"}, platform="bluesky")["handle"] == "ada.chen"
    )


def test_matching_embedded_webhook_sender_can_supply_its_own_recorded_username():
    record = {
        "sender_name": "123456",
        "sender_handle": "123456",
        "extra": {"sender": {"id": "123456", "username": "ada.chen"}},
    }
    assert sender_display(record, platform="instagram_login")["label"] == "@ada.chen"


@pytest.mark.parametrize("attribute", ["name", "username"])
@pytest.mark.parametrize("canonical", [True, False])
def test_embedded_attributes_for_different_sender_are_not_recovered(attribute, canonical):
    record = {
        "sender_handle": "123456",
        "extra": {"sender": {"id": "different-peer", attribute: "Private other sender"}},
    }
    if canonical:
        record["sender_id"] = "123456"
    display = sender_display(record, platform="instagram_login")
    assert display["label"] == "Unknown sender" and display["native_id"] == "123456"


def test_sender_display_never_consults_links_peers_bodies_or_unrelated_metadata():
    record = SimpleNamespace(
        sender_name="Unknown",
        sender_handle="123456",
        body="My name is not identity evidence",
        peer_name="Another person's name",
        extra={"from": {"name": "Unmatched from metadata"}, "username": "Unscoped username"},
    )
    assert sender_display(record, platform="facebook")["label"] == "Unknown sender"


@pytest.mark.parametrize("handle", [" 123456", "123456 ", "123\n456"])
def test_scoped_ids_are_never_normalized_into_another_id(handle):
    display = sender_display({"sender_handle": handle}, platform="facebook")
    assert display["native_id"] == display["handle"] == ""


@pytest.mark.django_db
@pytest.mark.parametrize("sender", [{"username": "ada.chen"}, {"name": "", "username": "ada.chen"}])
def test_real_meta_adapter_saves_username_when_name_is_absent(durable, sender):
    def respond(request):
        if request.url.path.endswith("/thread-1"):
            return httpx.Response(
                200,
                json={"id": "thread-1", "participants": {"data": [{"id": "page-1"}, {"id": "peer-1"}]}},
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "sender-name-message",
                        "from": {"id": "peer-1", **sender},
                        "to": {"data": [{"id": "page-1"}]},
                        "message": "Hello",
                        "created_time": (timezone.now() - timedelta(minutes=1)).isoformat(),
                    }
                ]
            },
        )

    result = run_one_page(checkpoint(durable).pk, MetaSyncAdapter(transport=httpx.MockTransport(respond)))
    assert result.status == "complete"
    stored = ConversationMessage.objects.get()
    assert stored.sender_name == "ada.chen" and stored.sender_id == "peer-1"


@pytest.mark.parametrize("name,expected", [(None, "ada.chen"), ("", "ada.chen"), ({"bogus": "name"}, "ada.chen")])
def test_webhook_normalizer_preserves_username_without_str_coercion(name, expected):
    account = SimpleNamespace(platform="instagram_login", account_platform_id="brand", webhook_target_id="")
    payload = {
        "sender": {"id": "123456", "name": name, "username": "ada.chen"},
        "recipient": {"id": "brand"},
        "message": {"mid": "message-1", "text": "Hello"},
    }
    with (
        patch("apps.inbox.sync_ingestion.canonical_owns_account", return_value=True),
        patch("apps.inbox.sync_ingestion.enqueue_message") as enqueue,
    ):
        assert ingest_meta_webhook(account, payload)
    observation = enqueue.call_args.args[1]
    assert observation.sender_name == expected and observation.sender_id == "123456"


@pytest.mark.django_db
def test_canonical_sender_and_peer_labels_never_use_native_ids(context):
    context.conversation.peer_id = "123456"
    context.conversation.save(update_fields=["peer_id"])
    message = row(context, direction="inbound", sender_id="123456", sender_name="123456")
    page = reader.read_conversation(context.scope, context.conversation.pk)
    assert page["conversation"]["peer_name"] == "Unknown sender"
    assert page["conversation"]["peer_handle"] == ""
    assert page["conversation"]["peer_native_id"] == "123456"
    assert page["messages"][0]["sender_name"] == "Unknown sender"
    assert page["messages"][0]["sender_handle"] == ""
    assert page["messages"][0]["sender_native_id"] == "123456"
    message.refresh_from_db()
    assert message.sender_id == message.sender_name == "123456"


@pytest.mark.django_db
def test_direct_peer_name_never_borrows_another_inbound_author(context):
    row(context, direction="inbound", sender_id="different-peer", sender_name="Another person's name")
    page = reader.read_conversation(context.scope, context.conversation.pk)
    assert page["conversation"]["peer_name"] == "Unknown sender"
    assert page["conversation"]["peer_native_id"] == "peer"


@pytest.mark.django_db
def test_direct_peer_uses_its_own_visible_name_over_newer_scoped_id_placeholder(context):
    row(
        context,
        direction="inbound",
        sender_id="peer",
        sender_name="Ada Chen",
        occurred_at=timezone.now() - timedelta(days=1),
    )
    row(context, direction="inbound", sender_id="peer", sender_name="peer")
    assert reader.read_conversation(context.scope, context.conversation.pk)["conversation"]["peer_name"] == "Ada Chen"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fault", ["workspace", "account", "platform", "conversation", "withdrawn", "expired", "generation"]
)
def test_peer_name_recovery_cannot_broaden_existing_scope_or_provenance(context, fault):
    row(context)
    candidate = row(context, direction="inbound", sender_id="peer", sender_name="Private identity")
    if fault == "workspace":
        candidate.workspace = Workspace.objects.create(
            name="Foreign", organization=context.account.workspace.organization
        )
    elif fault == "account":
        candidate.social_account = SocialAccount.objects.create(
            workspace=context.account.workspace, platform="facebook", account_platform_id="foreign-page"
        )
    elif fault == "platform":
        candidate.platform = "instagram_login"
    elif fault == "conversation":
        candidate.conversation = InboxConversation.objects.create(
            workspace=context.account.workspace,
            social_account=context.account,
            platform=context.account.platform,
            identity_kind="platform",
            platform_conversation_id="another-native-thread",
            peer_id="peer",
            conversation_type="direct",
            classification_reason="participants_pair",
        )
    else:
        change = {
            "withdrawn": {"withdrawn_at": timezone.now()},
            "expired": {"expired_at": timezone.now()},
            "generation": {"connection_generation": uuid4()},
        }[fault]
        proof(context, candidate, **change)
    candidate.save()
    page = reader.read_conversation(context.scope, context.conversation.pk)
    assert page["conversation"]["peer_name"] == "Unknown sender"


@pytest.mark.django_db
@pytest.mark.parametrize("kind,reason", [("group", "participants_group"), ("unknown", "participants_missing")])
def test_group_or_unknown_conversations_never_get_one_inbound_authors_identity(context, kind, reason):
    context.conversation.conversation_type = kind
    context.conversation.classification_reason = reason
    context.conversation.save(update_fields=["conversation_type", "classification_reason"])
    row(context, direction="inbound", sender_id="peer", sender_name="Ada Chen")
    value = reader.read_conversation(context.scope, context.conversation.pk)["conversation"]
    assert value["peer_name"] != "Ada Chen"
    assert value["peer_handle"] == value["peer_native_id"] == ""


@pytest.mark.django_db
@pytest.mark.parametrize("template", ["_message_row.html", "_message_panel.html", "_incoming_item.html"])
def test_legacy_templates_show_unknown_sender_without_fake_numeric_handle(inbox_account, template):
    message = InboxMessage.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        message_type="comment",
        platform_message_id="message-1",
        sender_name="123456789",
        sender_handle="123456789",
        body="Hello",
        received_at=timezone.now(),
    )
    html = render_to_string(
        f"inbox/partials/{template}",
        {"message": message, "incoming": message, "workspace": inbox_account.workspace},
    )
    assert "Unknown sender" in html and "@123456789" not in html
    if template == "_message_panel.html":
        assert "ID: 123456789" in html
