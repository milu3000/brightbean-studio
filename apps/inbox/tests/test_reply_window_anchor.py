"""Reply windows follow verified customer activity, not the selected bubble."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from apps.api_keys.services import issue_api_key
from apps.inbox.dm_send_gate import DMSendGateError
from apps.inbox.models import InboxMessage, InboxReply
from apps.inbox.services import create_reply_draft, reply_send_availability, validate_meta_reply_window
from apps.mcp.handlers import _send_reply
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2026, 10, 9, 9, tzinfo=UTC)


@pytest.fixture
def window(user, organization):
    workspace = Workspace.objects.create(name="Reply window anchors", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="facebook", account_platform_id="page-1", account_name="Offline account"
    )
    key = issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=user,
        name="Offline window test",
        permissions=["use_inbox", "reply_from_inbox"],
    ).api_key
    return SimpleNamespace(user=user, workspace=workspace, account=account, key=key, membership=membership)


def incoming(window, *, days=9, **overrides):
    fields = {
        "workspace": window.workspace,
        "social_account": window.account,
        "platform_message_id": str(uuid4()),
        "message_type": "dm",
        "sender_name": "Customer",
        "sender_handle": "peer-1",
        "body": "Customer question",
        "extra": {
            "conversation_id": "native-thread-1",
            "conversation_type": "direct",
            "classification_reason": "participants_pair",
        },
        "received_at": NOW - timedelta(days=days),
    }
    fields.update(overrides)
    return InboxMessage.objects.create(**fields)


@pytest.mark.parametrize("platform", ["facebook", "instagram_login"])
@pytest.mark.parametrize("surface", ["ui", "mcp_new", "mcp_draft"])
def test_new_customer_incoming_reopens_old_selected_message(window, client, platform, surface):
    window.account.platform = platform
    window.account.save(update_fields=["platform"])
    old = incoming(window)
    recent = incoming(window, days=0, received_at=NOW - timedelta(hours=1))
    provider = MagicMock()
    provider.reply_to_message.return_value = SimpleNamespace(platform_message_id="sent-1")
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services.get_provider", return_value=provider),
    ):
        assert reply_send_availability(old)["allowed"] is True
        if surface == "ui":
            client.force_login(window.user)
            response = client.post(f"/workspace/{window.workspace.pk}/inbox/{old.pk}/reply/", {"body": "Answer"})
            assert response.status_code == 200
        else:
            args = {"message_id": str(old.pk), "body": "Answer"}
            if surface == "mcp_draft":
                args = {"reply_id": str(create_reply_draft(message=old, body="Answer").pk)}
            _send_reply(args, {"api_key": window.key, "membership": window.membership})

    provider.reply_to_message.assert_called_once()
    assert provider.reply_to_message.call_args.kwargs["human_agent"] is False
    assert provider.reply_to_message.call_args.kwargs["message_id"] == old.platform_message_id
    reply = InboxReply.objects.get()
    assert reply.status == "sent" and reply.inbox_message_id == old.pk
    old.refresh_from_db()
    recent.refresh_from_db()
    assert old.received_at == NOW - timedelta(days=9)
    assert recent.received_at == NOW - timedelta(hours=1)


@pytest.mark.parametrize("mismatch", ["thread", "peer", "account", "workspace", "missing_thread", "numeric_thread"])
def test_other_identity_cannot_reopen_automated_reply_window(window, mismatch):
    old = incoming(window)
    recent = incoming(window, received_at=NOW - timedelta(hours=1))
    if mismatch == "thread":
        recent.extra["conversation_id"] = "another-thread"
    elif mismatch == "peer":
        recent.sender_handle = "another-peer"
    elif mismatch == "account":
        recent.social_account = SocialAccount.objects.create(
            workspace=window.workspace, platform="facebook", account_platform_id="page-2"
        )
    elif mismatch == "workspace":
        recent.workspace = Workspace.objects.create(name="Other", organization=window.workspace.organization)
    elif mismatch == "missing_thread":
        old.extra.pop("conversation_id")
    else:
        old.extra["conversation_id"] = "123"
        recent.extra["conversation_id"] = 123
    old.save()
    recent.save()
    with patch("apps.inbox.services.timezone.now", return_value=NOW), pytest.raises(ValueError):
        validate_meta_reply_window(old, automated=True)


@pytest.mark.parametrize(
    "invalid", ["future", "echo", "nested_echo", "outbound", "self", "deleted", "group", "unknown"]
)
def test_unverified_activity_cannot_reopen_automated_reply_window(window, invalid):
    old = incoming(window)
    recent = incoming(window, received_at=NOW - timedelta(hours=1))
    if invalid == "future":
        recent.received_at = NOW + timedelta(seconds=1)
    elif invalid == "echo":
        recent.extra["is_echo"] = True
    elif invalid == "nested_echo":
        recent.extra["message"] = {"is_echo": True}
    elif invalid == "outbound":
        recent.extra["direction"] = "outbound"
    elif invalid == "self":
        recent.sender_handle = window.account.account_platform_id
    elif invalid == "deleted":
        recent.extra["is_deleted"] = True
    else:
        recent.extra["conversation_type"] = invalid
        recent.extra["classification_reason"] = "participants_group" if invalid == "group" else "participants_missing"
    recent.save()
    with patch("apps.inbox.services.timezone.now", return_value=NOW), pytest.raises(ValueError):
        validate_meta_reply_window(old, automated=True)


@pytest.mark.parametrize(
    "received_at", [None, NOW.replace(tzinfo=None), datetime(1970, 1, 1, tzinfo=UTC), NOW + timedelta(seconds=1)]
)
def test_unknown_or_future_automated_timestamp_is_invalid_not_expired(window, received_at):
    message = incoming(window)
    message.received_at = received_at
    with patch("apps.inbox.services.timezone.now", return_value=NOW), pytest.raises(DMSendGateError) as held:
        validate_meta_reply_window(message, automated=True)
    assert held.value.code == "invalid_reply_window"
    assert "valid inbound message timestamp" in str(held.value)


@pytest.mark.parametrize("received_at", [datetime(1970, 1, 1, tzinfo=UTC), NOW + timedelta(seconds=1)])
def test_valid_customer_activity_anchors_window_when_selected_timestamp_is_unknown(window, received_at):
    selected = incoming(window, received_at=received_at)
    incoming(window, received_at=NOW - timedelta(hours=1))
    with patch("apps.inbox.services.timezone.now", return_value=NOW):
        assert validate_meta_reply_window(selected, automated=True) is None


@pytest.mark.parametrize("platform", ["facebook", "instagram_login"])
@pytest.mark.parametrize(
    "received_at", [NOW - timedelta(days=2), NOW - timedelta(days=8), datetime(1970, 1, 1, tzinfo=UTC)]
)
def test_manual_standard_reply_does_not_infer_platform_window_from_stored_age(window, platform, received_at):
    from apps.inbox.dm_send_gate import session_send_authorization
    from apps.inbox.services import send_reply_now
    from providers.facebook import FacebookProvider
    from providers.instagram_login import InstagramLoginProvider

    window.account.platform = platform
    window.account.save(update_fields=["platform"])
    message = incoming(window, received_at=received_at)
    reply = create_reply_draft(message=message, body="Human answer")
    provider = FacebookProvider({"page_id": "page-1"}) if platform == "facebook" else InstagramLoginProvider({})
    response = MagicMock()
    response.json.return_value = {"message_id": "platform-accepted"}
    with (
        patch("apps.inbox.services.timezone.now", return_value=NOW),
        patch("apps.inbox.services.get_provider", return_value=provider),
        patch.object(provider, "_request", return_value=response) as request,
    ):
        assert reply_send_availability(message, reply=reply)["allowed"]
        send_reply_now(reply, actor=window.user, authorization=session_send_authorization(window.user))
    payload = request.call_args.kwargs["json"]
    assert payload["messaging_type"] == "RESPONSE" and "tag" not in payload
    reply.refresh_from_db()
    assert reply.status == "sent" and reply.platform_reply_id == "platform-accepted"


@pytest.mark.parametrize("received_at", [None, NOW.replace(tzinfo=None), NOW + timedelta(seconds=1)])
def test_manual_timestamp_is_not_a_standalone_platform_rejection(window, received_at):
    message = incoming(window)
    message.received_at = received_at
    assert validate_meta_reply_window(message, automated=False) is None


@pytest.mark.parametrize("outcome", ["refused", "missing_receipt"])
def test_manual_old_message_still_requires_truthful_provider_outcome(window, outcome):
    from apps.inbox.dm_send_gate import session_send_authorization
    from apps.inbox.services import send_reply_now
    from providers.exceptions import APIError

    message = incoming(window)
    reply = create_reply_draft(message=message, body="Human answer")
    provider = MagicMock()
    if outcome == "refused":
        provider.reply_to_message.side_effect = APIError(
            "Synthetic policy refusal", platform="Facebook", status_code=403, raw_response={"error": {"code": 10}}
        )
    else:
        provider.reply_to_message.return_value = SimpleNamespace(platform_message_id="")
    with patch("apps.inbox.services.get_provider", return_value=provider), pytest.raises(DMSendGateError) as held:
        send_reply_now(reply, actor=window.user, authorization=session_send_authorization(window.user))
    provider.reply_to_message.assert_called_once()
    assert provider.reply_to_message.call_args.kwargs["human_agent"] is False
    reply.refresh_from_db()
    assert reply.status == ("failed" if outcome == "refused" else "unknown")
    assert held.value.code == ("not_sent" if outcome == "refused" else "outcome_unknown")
    assert reply.platform_reply_id == "" and reply.sent_at is None


def test_unmarked_echo_of_known_sent_id_cannot_reopen_automated_window(window):
    old = incoming(window)
    echo = incoming(window, received_at=NOW - timedelta(hours=1))
    InboxReply.objects.create(
        inbox_message=old, body="Previously sent", status="sent", platform_reply_id=echo.platform_message_id
    )
    with patch("apps.inbox.services.timezone.now", return_value=NOW), pytest.raises(ValueError, match="24 hours"):
        validate_meta_reply_window(old, automated=True)
