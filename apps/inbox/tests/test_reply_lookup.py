"""One bounded, read-only provider check returns receipt metadata only."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.inbox.dm_send_gate import DMSendGateError
from apps.inbox.models import ConversationMessage, InboxReply, InternalNote
from apps.inbox.reply_lookup import lookup_reply_receipts
from apps.inbox.tests.test_reply_reconciliation import unresolved as unresolved  # noqa: F401
from apps.inbox.tests.test_shared_reply_safety import dm as dm  # noqa: F401

pytestmark = pytest.mark.django_db


def payload(dm, reply):
    own, peer = dm.account.account_platform_id, "peer-1"
    dm.message.extra = {**dm.message.extra, "conversation_id": "conversation-1", "sender_id": peer}
    dm.message.save(update_fields=["extra"])
    return {
        "id": "conversation-1",
        "participants": {"data": [{"id": own}, {"id": peer}]},
        "messages": {
            "data": [
                {
                    "id": "candidate-receipt",
                    "message": reply.body,
                    "from": {"id": own},
                    "to": {"data": [{"id": peer}]},
                    "created_time": timezone.now().isoformat(),
                }
            ],
            "paging": {"next": "https://untrusted.example/do-not-follow"},
        },
    }


def lookup(dm, reply, provider):
    with patch("apps.inbox.reply_lookup.get_provider", return_value=provider):
        return lookup_reply_receipts(
            reply=reply,
            actor=dm.user,
            expected_updated_at=reply.updated_at,
            expected_send_generation=reply.send_generation,
        )


def provider_for(data):
    provider = Mock()
    provider._request.return_value.json.return_value = data
    return provider


@pytest.mark.parametrize(
    "platform,host", [("facebook", "graph.facebook.com"), ("instagram_login", "graph.instagram.com")]
)
def test_lookup_reads_one_known_thread_and_never_stores_remote_content(dm, unresolved, platform, host):
    dm.account.platform = platform
    dm.account.save(update_fields=["platform"])
    data = payload(dm, unresolved)
    data["messages"]["data"].append({"id": "foreign-content", "message": "Private unrelated remote body"})
    provider = provider_for(data)
    before = InboxReply.objects.values().get(pk=unresolved.pk)
    result = lookup(dm, unresolved, provider)
    assert result["status"] == "candidates" and result["more_available"] is True
    assert result["candidates"] == [
        {"platform_reply_id": "candidate-receipt", "sent_at": data["messages"]["data"][0]["created_time"]}
    ]
    call = provider._request.call_args
    assert call.args == ("GET", f"https://{host}/v25.0/conversation-1")
    assert "messages.limit(100)" in call.kwargs["params"]["fields"]
    provider._request.assert_called_once()
    assert "Private unrelated" not in str(result) and unresolved.body not in str(result)
    assert InboxReply.objects.values().get(pk=unresolved.pk) == before
    assert not InternalNote.objects.exists() and not ConversationMessage.objects.exists()


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_thread",
        "group",
        "partial_participants",
        "incoming",
        "wrong_peer",
        "old_time",
        "future_time",
        "other_text",
        "bad_to",
        "missing_id",
        "empty",
    ],
)
def test_ambiguous_missing_or_wrong_receipt_never_becomes_not_sent(dm, unresolved, fault):
    data = payload(dm, unresolved)
    row = data["messages"]["data"][0]
    if fault == "wrong_thread":
        data["id"] = "foreign-thread"
    elif fault == "group":
        data["participants"]["data"].append({"id": "third"})
    elif fault == "partial_participants":
        data["participants"]["paging"] = {"next": "not-followed"}
    elif fault == "incoming":
        row["from"]["id"] = "peer-1"
    elif fault == "wrong_peer":
        row["to"]["data"][0]["id"] = "other-peer"
    elif fault == "old_time":
        row["created_time"] = (unresolved.created_at - timedelta(days=1)).isoformat()
    elif fault == "future_time":
        row["created_time"] = (timezone.now() + timedelta(days=1)).isoformat()
    elif fault == "other_text":
        row["message"] = "A different answer"
    elif fault == "bad_to":
        row["to"] = {"data": None, "paging": None}
    elif fault == "missing_id":
        row.pop("id")
    else:
        data["messages"]["data"] = []
    result = lookup(dm, unresolved, provider_for(data))
    assert result["status"] == "unconfirmed" and result["candidates"] == []
    assert "do not prove" in result["reason"]
    unresolved.refresh_from_db()
    assert unresolved.status == "unknown" and not unresolved.not_sent_verified


def test_lookup_caps_receipts_and_does_not_paginate(dm, unresolved):
    data = payload(dm, unresolved)
    template = deepcopy(data["messages"]["data"][0])
    data["messages"]["data"] = [{**template, "id": f"match-{i}"} for i in range(110)]
    provider = provider_for(data)
    result = lookup(dm, unresolved, provider)
    assert len(result["candidates"]) == 5 and result["more_available"] is True
    provider._request.assert_called_once()


@pytest.mark.parametrize("change", ["authority", "receipt", "native_id", "account_identity"])
def test_lookup_rechecks_authority_and_identity_after_network(dm, unresolved, change):
    data = payload(dm, unresolved)
    provider = provider_for(data)

    def receive(*args, **kwargs):
        if change == "authority":
            dm.member.delete()
        elif change == "receipt":
            InboxReply.objects.filter(pk=unresolved.pk).update(updated_at=timezone.now())
        elif change == "native_id":
            dm.message.extra["conversation_id"] = "changed"
            dm.message.save(update_fields=["extra"])
        else:
            dm.account.account_platform_id = "changed-own"
            dm.account.save(update_fields=["account_platform_id"])
        return Mock(json=lambda: data)

    provider._request.side_effect = receive
    with pytest.raises(DMSendGateError):
        lookup(dm, unresolved, provider)
    assert not InternalNote.objects.exists()


def test_provider_failure_is_unconfirmed_without_diagnostics_or_retry(dm, unresolved):
    payload(dm, unresolved)
    provider = provider_for(None)
    provider._request.side_effect = RuntimeError("private-provider-body-and-token")
    result = lookup(dm, unresolved, provider)
    assert result["status"] == "unconfirmed" and "private-provider" not in str(result)
    provider._request.assert_called_once()


@pytest.mark.parametrize("native", ["", "thread/other", "thread?access_token=synthetic", "thread%2fother", None])
def test_no_known_safe_native_thread_means_no_network(dm, unresolved, native):
    dm.message.extra = {**dm.message.extra, "conversation_id": native}
    dm.message.save(update_fields=["extra"])
    provider = provider_for(None)
    result = lookup(dm, unresolved, provider)
    assert result["status"] == "unconfirmed"
    provider._request.assert_not_called()


@pytest.mark.parametrize("generation", [None, True, -1, 1, "0"])
def test_stale_or_invalid_generation_never_calls_provider(dm, unresolved, generation):
    payload(dm, unresolved)
    with patch("apps.inbox.reply_lookup.get_provider") as provider, pytest.raises(DMSendGateError):
        lookup_reply_receipts(
            reply=unresolved,
            actor=dm.user,
            expected_updated_at=unresolved.updated_at,
            expected_send_generation=generation,
        )
    provider.assert_not_called()


def test_new_generation_during_lookup_cannot_reuse_same_timestamp(dm, unresolved):
    data = payload(dm, unresolved)
    provider = provider_for(data)

    def newer_attempt(*args, **kwargs):
        InboxReply.objects.filter(pk=unresolved.pk).update(send_generation=unresolved.send_generation + 1)
        return Mock(json=lambda: data)

    provider._request.side_effect = newer_attempt
    with pytest.raises(DMSendGateError):
        lookup(dm, unresolved, provider)
    assert not InternalNote.objects.exists()
