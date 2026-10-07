"""Synthetic provider failures: safe diagnostics must not change send receipts."""

import json
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from django.utils import timezone

from apps.inbox import dm_send_gate as gate
from apps.inbox import services
from apps.inbox.models import DMSendAttempt, InboxMessage
from apps.inbox.provider_failures import log_dm_provider_failure, provider_failure_diagnostics
from apps.members.models import WorkspaceMembership
from providers.exceptions import APIError, QuotaExceededError, RateLimitError, TokenExpiredError
from providers.facebook import FacebookProvider
from providers.instagram_login import InstagramLoginProvider


@pytest.mark.parametrize("provider", [FacebookProvider, InstagramLoginProvider])
def test_actual_adapter_failure_keeps_only_allowlisted_diagnostics(provider, caplog):
    error = provider()._error_for_response(
        httpx.Response(
            403,
            json={
                "error": {
                    "code": 10,
                    "error_subcode": 2534022,
                    "message": "PRIVATE_BODY access_token=DO_NOT_LOG",
                    "fbtrace_id": "PRIVATE_TRACE",
                    "error_data": {"recipient": "PRIVATE_PEER"},
                }
            },
        )
    )
    expected = {"http_status": 403, "code": 10, "subcode": 2534022, "category": "permission_or_policy"}
    assert provider_failure_diagnostics(error) == expected
    reply_id = uuid4()
    log_dm_provider_failure(reply_id, error)
    record = caplog.records[-1]
    assert record.exc_info is None
    assert json.loads(record.getMessage().split("DM provider failure ", 1)[1]) == {
        "reply_id": str(reply_id),
        **expected,
    }
    for secret in ("PRIVATE_BODY", "DO_NOT_LOG", "PRIVATE_TRACE", "PRIVATE_PEER", "access_token"):
        assert secret not in caplog.text
        assert secret not in services.reply_failure_reason(error)
    assert "reconnect" not in services.reply_failure_reason(error).lower()


@pytest.mark.parametrize("value", [True, "190", "token-secret", -1, 2**64, [], {}, None])
def test_untrusted_diagnostic_values_are_not_logged(value):
    error = APIError("PRIVATE", status_code=value, raw_response={"error": {"code": value, "error_subcode": value}})
    assert provider_failure_diagnostics(error) == {
        "http_status": None,
        "code": None,
        "subcode": None,
        "category": "unknown",
    }


@pytest.mark.parametrize("raw", [None, [], "SECRET", {"error": "SECRET"}, {"error": []}])
def test_malformed_provider_envelopes_are_safe(raw):
    error = APIError("SECRET", status_code=403, raw_response=raw)
    assert provider_failure_diagnostics(error) == {
        "http_status": 403,
        "code": None,
        "subcode": None,
        "category": "permission_or_policy",
    }


def test_quota_status_is_preserved_and_statusless_rate_limit_is_not_invented():
    error = QuotaExceededError("private", status_code=403)
    assert error.status_code == 403
    assert provider_failure_diagnostics(error)["http_status"] == 403
    assert provider_failure_diagnostics(error)["category"] == "rate_limit"
    assert provider_failure_diagnostics(RateLimitError("private"))["http_status"] is None


def test_explicit_expiration_keeps_evidence_based_reconnect_hint():
    error = TokenExpiredError("PRIVATE_TOKEN", status_code=401)
    assert provider_failure_diagnostics(error)["category"] == "authentication_expired"
    assert "Reconnect the account" in services.reply_failure_reason(error)
    assert "PRIVATE_TOKEN" not in services.reply_failure_reason(error)


@pytest.mark.parametrize("provider", [FacebookProvider, InstagramLoginProvider])
def test_rate_limit_adapter_does_not_log_raw_body(provider, caplog):
    error = provider()._error_for_response(
        httpx.Response(
            429,
            json={"error": {"message": "PRIVATE_TOKEN", "code": 4}},
        )
    )
    assert error.status_code == 429
    assert provider_failure_diagnostics(error)["http_status"] == 429
    assert "PRIVATE_TOKEN" not in caplog.text


@pytest.mark.parametrize(
    "error,category,phrase",
    [
        (APIError("private", status_code=401), "authentication", "authorization"),
        (
            APIError("private", status_code=400, platform="Instagram (Direct)", raw_response={"error": {"code": 190}}),
            "authentication",
            "authorization",
        ),
        (APIError("private", status_code=403), "permission_or_policy", "permissions or messaging rules"),
        (RateLimitError("private"), "rate_limit", "rate limit"),
        (APIError("private", status_code=429), "rate_limit", "rate limit"),
        (APIError("private", status_code=503), "temporary", "Check delivery status"),
        (APIError("private", status_code=400), "request_rejected", "Review the reply"),
        (httpx.ReadTimeout("https://example.invalid/?access_token=PRIVATE"), "unknown", "delivery status"),
    ],
)
def test_reasons_are_category_specific_without_exposing_error_text(error, category, phrase):
    assert provider_failure_diagnostics(error)["category"] == category
    reason = services.reply_failure_reason(error)
    assert phrase in reason
    assert "private" not in reason.lower()
    assert "reconnect" not in reason.lower()


@pytest.fixture
def diagnostic_reply(inbox_account, user, org_owner, request):
    WorkspaceMembership.objects.create(user=user, workspace=inbox_account.workspace, workspace_role="owner")
    enrolled, platform = request.param
    inbox_account.platform = platform
    inbox_account.save(update_fields=["platform"])
    if enrolled:
        control = gate.enroll_dm_send_control(
            account_id=inbox_account.pk,
            workspace_id=inbox_account.workspace_id,
            platform=platform,
            account_platform_id=inbox_account.account_platform_id,
        )
        gate.set_dm_send_paused(
            account_id=inbox_account.pk,
            workspace_id=inbox_account.workspace_id,
            paused=False,
            expected_epoch=control.epoch,
        )
    message = InboxMessage.objects.create(
        workspace=inbox_account.workspace,
        social_account=inbox_account,
        platform_message_id="synthetic-incoming",
        message_type="dm",
        sender_handle="synthetic-peer",
        body="PRIVATE_CUSTOMER_BODY",
        received_at=timezone.now(),
        extra={"conversation_type": "direct", "classification_reason": "participants_pair"},
    )
    reply = services.create_reply_draft(message=message, body="PRIVATE_REPLY_BODY", author=user)
    return reply, user, enrolled, platform


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "diagnostic_reply",
    [
        (False, "facebook"),
        (False, "instagram_login"),
        (True, "facebook"),
        (True, "instagram_login"),
    ],
    indirect=True,
)
@pytest.mark.parametrize(
    "status,expected", [(401, "failed"), (403, "failed"), (400, "unknown"), (429, "unknown"), (503, "unknown")]
)
def test_diagnostics_never_expand_known_refusal_or_retry_policy(diagnostic_reply, status, expected, caplog):
    reply, user, enrolled, platform = diagnostic_reply
    provider = InstagramLoginProvider() if platform == "instagram_login" else FacebookProvider()
    error = provider._error_for_response(
        httpx.Response(
            status,
            json={"error": {"code": 10, "error_subcode": 2534022, "message": "PRIVATE_PROVIDER_BODY"}},
        )
    )

    def refused(*args, **kwargs):
        kwargs["before_provider"]()
        raise error

    def send():
        return services.send_reply_now(reply, actor=user, authorization=gate.session_send_authorization(user))

    caplog.clear()
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=refused), pytest.raises(gate.DMSendGateError):
        send()
    reply.refresh_from_db()
    assert reply.status == expected
    assert reply.not_sent_verified is (expected == "failed")
    assert not reply.platform_reply_id
    entries = [record for record in caplog.records if record.name == "apps.inbox.provider_failures"]
    assert len(entries) == 1
    payload = json.loads(entries[0].getMessage().split("DM provider failure ", 1)[1])
    assert payload["reply_id"] == str(reply.pk)
    assert payload["http_status"] == status
    assert payload["code"] == 10 and payload["subcode"] == 2534022
    for private in ("PRIVATE_PROVIDER_BODY", "PRIVATE_CUSTOMER_BODY", "PRIVATE_REPLY_BODY", "synthetic-peer"):
        assert private not in entries[0].getMessage()
        assert private not in reply.send_error
    if status == 403:
        assert "permissions or messaging rules" in reply.send_error
    if enrolled:
        assert DMSendAttempt.objects.get(reply=reply).outcome == ("not_sent" if expected == "failed" else "unknown")
    if expected == "unknown":
        with patch("apps.inbox.services._dispatch_to_platform") as retry, pytest.raises(services.ReplyStateError):
            send()
        retry.assert_not_called()
    else:

        def accepted(*args, **kwargs):
            kwargs["before_provider"]()
            return "synthetic-accepted"

        with patch("apps.inbox.services._dispatch_to_platform", side_effect=accepted) as retry:
            send()
        retry.assert_called_once()
        reply.refresh_from_db()
        assert reply.status == "sent"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("diagnostic_reply", [(False, "instagram_login"), (True, "instagram_login")], indirect=True)
def test_diagnostic_log_failure_does_not_change_definitive_receipt(diagnostic_reply):
    reply, user, _, _ = diagnostic_reply

    def refused(*args, **kwargs):
        kwargs["before_provider"]()
        raise APIError("private", platform="Instagram (Direct)", status_code=403)

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=refused),
        patch("apps.inbox.provider_failures.logger.warning", side_effect=RuntimeError("logger offline")),
        pytest.raises(gate.DMSendGateError),
    ):
        services.send_reply_now(reply, actor=user, authorization=gate.session_send_authorization(user))
    reply.refresh_from_db()
    assert reply.status == "failed" and reply.not_sent_verified
