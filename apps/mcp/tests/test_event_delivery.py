"""Transport security tests use synthetic signing keys and mocked networking."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import socket
import ssl
import threading
import time
from unittest.mock import MagicMock

import pytest

from apps.mcp import event_delivery as delivery

CALLBACK = "https://receiver.example.com/hooks/events?token=synthetic-test-only"
SECRET = "whsec_" + base64.b64encode(b"synthetic-test-signing-key-000001").decode("ascii")
PREVIOUS_SECRET = "whsec_" + base64.b64encode(b"synthetic-test-signing-key-000002").decode("ascii")


def dns_address(address: str) -> tuple:
    if ":" in address:
        return (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))


@pytest.fixture(autouse=True)
def no_network(monkeypatch, settings):
    settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS = ["receiver.example.com"]
    monkeypatch.setattr(delivery.socket, "getaddrinfo", MagicMock(side_effect=AssertionError("Unexpected DNS lookup")))
    monkeypatch.setattr(
        delivery.socket, "socket", MagicMock(side_effect=AssertionError("Unexpected network connection"))
    )


@pytest.fixture
def wire(monkeypatch):
    response = MagicMock(status=202)
    response.read1.side_effect = io.BytesIO(b'{"accepted":true}').read1
    connection = MagicMock()
    connection.deadline = time.monotonic() + 10
    connection.timed_out = threading.Event()
    connection.getresponse.return_value = response
    factory = MagicMock(return_value=connection)
    monkeypatch.setattr(delivery, "_PinnedHTTPSConnection", factory)
    return connection, response, factory


def assert_reason(reason, fn, *args, **kwargs):
    with pytest.raises(delivery.CallbackError) as caught:
        fn(*args, **kwargs)
    assert caught.value.reason == reason
    assert str(caught.value) == "Callback request failed."
    assert CALLBACK not in str(caught.value)
    assert SECRET not in str(caught.value)
    return caught.value


def test_url_normalizes_only_scheme_hostname_and_default_port():
    assert delivery.validate_callback_url("HTTPS://RECEIVER.EXAMPLE.COM:443/hook?b=2&a=1") == (
        "https://receiver.example.com/hook?b=2&a=1"
    )
    assert delivery.validate_callback_url("https://receiver.example.com") == "https://receiver.example.com/"
    delivery.socket.getaddrinfo.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "http://receiver.example.com/hooks",
        "ftp://receiver.example.com/hooks",
        "//receiver.example.com/hooks",
        "https://user@receiver.example.com/hooks",
        "https://user:password@receiver.example.com/hooks",
        "https://@receiver.example.com/hooks",
        "https://receiver.example.com/hooks#",
        "https://receiver.example.com/hooks#fragment",
        "https://receiver.example.com:80/hooks",
        "https://receiver.example.com:444/hooks",
        "https://receiver.example.com:0443/hooks",
        "https://receiver.example.com:/hooks",
        "https://receiver.example.com:bad/hooks",
        "https://receiver.example.com:99999/hooks",
        "https://receiver.example.com./hooks",
        "https://receiver..example.com/hooks",
        "https://-receiver.example.com/hooks",
        "https://receiver.example.com\\@attacker.example/hooks",
        " https://receiver.example.com/hooks",
        "https://receiver.example.com/white space",
        "https://receiver.example.com/\r\nInjected:value",
        "https://receiver.example.com/\x00value",
        "https://receiver.example.com/\x7fvalue",
        "https://receiver.example.com/未編碼",
        "https://[::1/hooks",
        "",
        None,
        1,
        "https://receiver.example.com/" + "a" * delivery.MAX_CALLBACK_URL_LENGTH,
    ],
)
def test_invalid_callback_urls_fail_before_dns(url):
    assert_reason("invalid_callback_url", delivery.validate_callback_url, url)
    delivery.socket.getaddrinfo.assert_not_called()


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "8.8.8.8",
        "127.1",
        "2130706433",
        "0177.0.0.1",
        "0x7f.0x0.0x0.0x1",
        "[::1]",
        "[2606:4700:4700::1111]",
        "[fe80::1%25eth0]",
        "localhost",
        "service.localhost",
        "service.local",
        "service.localdomain",
        "metadata.internal",
        "service.lan",
        "service.home.arpa",
        "intranet",
    ],
)
def test_ip_literals_and_local_hosts_remain_forbidden_when_allowlisted(settings, host):
    settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS = [host]
    assert_reason("invalid_callback_url", delivery.validate_callback_url, f"https://{host}/hooks")


@pytest.mark.parametrize("configured", [[], (), "", None, True, ["*"], ["*.example.com"], ["example.com"], [CALLBACK]])
def test_host_allowlist_fails_closed_and_uses_exact_hostnames(settings, configured):
    settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS = configured
    assert_reason("callback_host_not_allowed", delivery.validate_callback_url, CALLBACK)


def test_allowlist_does_not_match_subdomains_or_suffix_lookalikes():
    for host in ("child.receiver.example.com", "receiver.example.com.attacker.example", "evilreceiver.example.com"):
        assert_reason("callback_host_not_allowed", delivery.validate_callback_url, f"https://{host}/")


def test_allowlist_accepts_comma_separated_configuration(settings):
    settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS = " RECEIVER.EXAMPLE.COM,other.example.com "
    assert delivery.validate_callback_url(CALLBACK) == CALLBACK


@pytest.mark.parametrize("length", [24, 25, 32, 48, 63, 64])
def test_standard_webhooks_key_size_boundaries(length):
    key = bytes(range(length))
    assert delivery.validate_secret("whsec_" + base64.b64encode(key).decode("ascii")) == key


@pytest.mark.parametrize(
    "secret",
    [
        None,
        b"whsec_test",
        "",
        "whsec_",
        "notwhsec_" + base64.b64encode(b"a" * 32).decode("ascii"),
        "whsec_" + base64.b64encode(b"a" * 23).decode("ascii"),
        "whsec_" + base64.b64encode(b"a" * 65).decode("ascii"),
        SECRET + "=",
        SECRET + "\n",
        SECRET.replace("_", "_!", 1),
        "whsec_" + "非" * 44,
    ],
)
def test_malformed_signing_secrets_are_rejected(secret):
    assert_reason("invalid_secret", delivery.validate_secret, secret)


def test_serialization_is_stable_compact_utf8():
    assert delivery.serialize_payload({"z": "中文", "a": {"b": 2, "a": 1}}) == (
        '{"a":{"a":1,"b":2},"z":"中文"}'.encode()
    )
    assert delivery.canonical_json({"b": 2, "a": 1}) == delivery.canonical_json({"a": 1, "b": 2})


@pytest.mark.parametrize("value", [{"x": float("nan")}, {"x": float("inf")}, {"x": object()}, {"x": "\ud800"}])
def test_serialization_rejects_non_json_values(value):
    assert_reason("invalid_payload", delivery.serialize_payload, value)


def test_serialization_enforces_utf8_byte_limit():
    assert_reason("payload_too_large", delivery.serialize_payload, {"text": "中文" * (delivery.MAX_PAYLOAD_BYTES // 3)})


def test_public_dns_addresses_are_pinned_while_tls_checks_original_host(monkeypatch):
    getaddrinfo = MagicMock(return_value=[dns_address("1.1.1.1")])
    raw_socket = MagicMock()
    monkeypatch.setattr(delivery.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(delivery.socket, "socket", MagicMock(return_value=raw_socket))
    connection = delivery._PinnedHTTPSConnection("receiver.example.com", deadline=time.monotonic() + 10)
    context = connection._context
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    tls_socket = MagicMock()
    wrap = MagicMock(return_value=tls_socket)
    monkeypatch.setattr(context, "wrap_socket", wrap)

    connection.connect()

    getaddrinfo.assert_called_once_with(
        "receiver.example.com", 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    raw_socket.connect.assert_called_once_with(("1.1.1.1", 443))
    wrap.assert_called_once_with(raw_socket, server_hostname="receiver.example.com")
    assert connection.host == "receiver.example.com"
    assert connection.sock is tls_socket


def test_public_ipv6_address_can_be_pinned(monkeypatch):
    address = "2606:4700:4700::1111"
    monkeypatch.setattr(delivery.socket, "getaddrinfo", MagicMock(return_value=[dns_address(address)]))
    raw_socket = MagicMock()
    monkeypatch.setattr(delivery.socket, "socket", MagicMock(return_value=raw_socket))
    connection = delivery._PinnedHTTPSConnection("receiver.example.com", deadline=time.monotonic() + 10)
    monkeypatch.setattr(connection._context, "wrap_socket", MagicMock(return_value=MagicMock()))
    connection.connect()
    raw_socket.connect.assert_called_once_with((address, 443, 0, 0))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "255.255.255.255",
        "240.0.0.1",
        "::",
        "::1",
        "fd00::1",
        "fe80::1",
        "ff02::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "::ffff:8.8.8.8",
        "2002:7f00:1::",
        "64:ff9b::7f00:1",
        "64:ff9b:1::7f00:1",
    ],
)
def test_non_public_or_transition_destinations_fail_closed(monkeypatch, address):
    monkeypatch.setattr(delivery.socket, "getaddrinfo", MagicMock(return_value=[dns_address(address)]))
    connection = delivery._PinnedHTTPSConnection("receiver.example.com", deadline=time.monotonic() + 10)
    assert_reason("unsafe_address", connection.connect)
    delivery.socket.socket.assert_not_called()


def test_mixed_public_private_dns_never_opens_a_socket(monkeypatch):
    monkeypatch.setattr(
        delivery.socket, "getaddrinfo", MagicMock(return_value=[dns_address("1.1.1.1"), dns_address("127.0.0.1")])
    )
    connection = delivery._PinnedHTTPSConnection("receiver.example.com", deadline=time.monotonic() + 10)
    assert_reason("unsafe_address", connection.connect)
    delivery.socket.socket.assert_not_called()


def test_reconnect_reresolves_and_blocks_dns_rebinding(monkeypatch):
    getaddrinfo = MagicMock(side_effect=[[dns_address("1.1.1.1")], [dns_address("127.0.0.1")]])
    monkeypatch.setattr(delivery.socket, "getaddrinfo", getaddrinfo)
    socket_factory = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(delivery.socket, "socket", socket_factory)
    connection = delivery._PinnedHTTPSConnection("receiver.example.com", deadline=time.monotonic() + 10)
    monkeypatch.setattr(connection._context, "wrap_socket", MagicMock(return_value=MagicMock()))
    connection.connect()
    connection.close()
    assert_reason("unsafe_address", connection.connect)
    assert getaddrinfo.call_count == 2
    assert socket_factory.call_count == 1


def test_allowlist_is_checked_again_at_connection_time(settings):
    connection = delivery._PinnedHTTPSConnection("receiver.example.com", deadline=time.monotonic() + 10)
    settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS = []
    assert_reason("callback_host_not_allowed", connection.connect)
    delivery.socket.getaddrinfo.assert_not_called()


@pytest.mark.parametrize("mode", ["empty", "error", "timeout"])
def test_dns_errors_are_categorized_without_sensitive_details(monkeypatch, mode):
    resolver = MagicMock(return_value=[])
    if mode == "error":
        resolver.side_effect = socket.gaierror(CALLBACK)
    elif mode == "timeout":
        resolver.side_effect = TimeoutError(CALLBACK)
    monkeypatch.setattr(delivery.socket, "getaddrinfo", resolver)
    assert_reason(
        "timeout" if mode == "timeout" else "dns_failed",
        delivery._resolve_public_addresses,
        "receiver.example.com",
        time.monotonic() + 10,
    )


def test_dns_wait_is_bounded_and_releases_slot_when_work_finishes(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_lookup(*args, **kwargs):
        entered.set()
        try:
            release.wait(timeout=2)
            return [dns_address("1.1.1.1")]
        finally:
            finished.set()

    monkeypatch.setattr(delivery.socket, "getaddrinfo", blocked_lookup)
    try:
        assert_reason("timeout", delivery._resolve_public_addresses, "receiver.example.com", time.monotonic() + 0.03)
        assert entered.is_set()
    finally:
        release.set()
        assert finished.wait(timeout=2)


def test_timer_shuts_down_response_socket_even_after_connection_releases_it():
    connection = delivery._PinnedHTTPSConnection("receiver.example.com", deadline=time.monotonic() + 10)
    transport = MagicMock()
    connection._transport_socket = transport
    connection.sock = None  # HTTPConnection does this for Connection: close responses.
    connection.abort_timeout()
    assert connection.timed_out.is_set()
    transport.shutdown.assert_called_once_with(socket.SHUT_RDWR)
    transport.close.assert_called_once()


def test_proxy_tunnelling_is_blocked():
    connection = delivery._PinnedHTTPSConnection("receiver.example.com", deadline=time.monotonic() + 10)
    connection.set_tunnel("private.example.com")
    assert_reason("invalid_callback_url", connection.connect)
    delivery.socket.getaddrinfo.assert_not_called()


def test_signed_post_uses_exact_bytes_and_standard_headers(monkeypatch, wire):
    connection, response, _factory = wire
    monkeypatch.setattr(delivery.time, "time", lambda: 1790956800.75)
    body = b'{ "eventId": "evt_123", "data": {"literal": "preserve spaces"} }'
    result = delivery.post_signed(CALLBACK, SECRET, "sub_123", "evt_123", body)
    args, kwargs = connection.request.call_args
    assert args == ("POST", "/hooks/events?token=synthetic-test-only")
    assert kwargs["body"] is body
    headers = kwargs["headers"]
    signed = b"evt_123.1790956800." + body
    expected = base64.b64encode(hmac.new(delivery.validate_secret(SECRET), signed, hashlib.sha256).digest()).decode()
    assert headers == {
        "Content-Type": "application/json",
        "webhook-id": "evt_123",
        "webhook-timestamp": "1790956800",
        "webhook-signature": "v1," + expected,
        "X-MCP-Subscription-Id": "sub_123",
        "Connection": "close",
    }
    assert result == delivery.CallbackResponse(status=202, body=b'{"accepted":true}')
    assert "accepted" not in repr(result)
    connection.close.assert_called_once()
    response.close.assert_called_once()


def test_rotation_produces_space_separated_signatures(monkeypatch, wire):
    connection, _response, _factory = wire
    monkeypatch.setattr(delivery.time, "time", lambda: 1000)
    body = b'{"eventId":"evt_123"}'
    delivery.post_signed(CALLBACK, SECRET, "sub_123", "evt_123", body, previous_secret=PREVIOUS_SECRET)
    signatures = connection.request.call_args.kwargs["headers"]["webhook-signature"].split(" ")
    assert len(signatures) == 2
    for secret, signature in zip([SECRET, PREVIOUS_SECRET], signatures, strict=True):
        key = delivery.validate_secret(secret)
        assert signature == "v1," + base64.b64encode(hmac.digest(key, b"evt_123.1000." + body, "sha256")).decode()


def test_retry_preserves_event_bytes_but_gets_fresh_timestamp_signature(monkeypatch, wire):
    connection, _response, _factory = wire
    monkeypatch.setattr(delivery.time, "time", MagicMock(side_effect=[1000, 1001]))
    body = b'{"eventId":"evt_123"}'
    delivery.post_signed(CALLBACK, SECRET, "sub_123", "evt_123", body)
    delivery.post_signed(CALLBACK, SECRET, "sub_123", "evt_123", body)
    first, second = [call.kwargs for call in connection.request.call_args_list]
    assert first["body"] is second["body"] is body
    assert first["headers"]["webhook-id"] == second["headers"]["webhook-id"] == "evt_123"
    assert first["headers"]["webhook-timestamp"] == "1000"
    assert second["headers"]["webhook-timestamp"] == "1001"
    assert first["headers"]["webhook-signature"] != second["headers"]["webhook-signature"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_fail_without_another_request_or_read(wire, status):
    connection, response, _factory = wire
    response.status = status
    response.headers = {"Location": "http://127.0.0.1/internal"}
    assert_reason("redirect_blocked", delivery.post_signed, CALLBACK, SECRET, "sub_1", "evt_1", b"{}")
    connection.request.assert_called_once()
    response.read1.assert_not_called()
    response.close.assert_called_once()


@pytest.mark.parametrize("status", [204, 400, 410, 413, 429, 500, 503])
def test_non_redirect_statuses_are_returned_to_the_retry_policy(wire, status):
    _connection, response, _factory = wire
    response.status = status
    assert delivery.post_signed(CALLBACK, SECRET, "sub_1", "evt_1", b"{}").status == status


@pytest.mark.parametrize("payload", ["{}", bytearray(b"{}"), None])
def test_post_requires_pre_serialized_bytes(wire, payload):
    connection, _response, _factory = wire
    assert_reason("invalid_payload", delivery.post_signed, CALLBACK, SECRET, "sub_1", "evt_1", payload)
    connection.request.assert_not_called()


def test_oversized_request_fails_before_network(wire):
    connection, _response, _factory = wire
    assert_reason(
        "payload_too_large",
        delivery.post_signed,
        CALLBACK,
        SECRET,
        "sub_1",
        "evt_1",
        b"x" * (delivery.MAX_PAYLOAD_BYTES + 1),
    )
    connection.request.assert_not_called()


@pytest.mark.parametrize(
    "event_id,subscription_id", [("evt\r\nInjected: yes", "sub"), ("evt", "sub\nInjected"), ("", "sub")]
)
def test_identifiers_cannot_inject_headers(wire, event_id, subscription_id):
    connection, _response, _factory = wire
    assert_reason("invalid_identifier", delivery.post_signed, CALLBACK, SECRET, subscription_id, event_id, b"{}")
    connection.request.assert_not_called()


def test_response_body_is_bounded_even_without_content_length(wire):
    connection, response, _factory = wire
    source = io.BytesIO(b"x" * (delivery.MAX_RESPONSE_BYTES + 100))
    response.read1.side_effect = source.read1
    assert_reason("response_too_large", delivery.post_signed, CALLBACK, SECRET, "sub_1", "evt_1", b"{}")
    assert source.tell() == delivery.MAX_RESPONSE_BYTES + 1
    connection.close.assert_called_once()
    response.close.assert_called_once()


@pytest.mark.parametrize(
    "error,reason",
    [
        (TimeoutError(CALLBACK), "timeout"),
        (ssl.SSLError(CALLBACK), "tls_failed"),
        (OSError(CALLBACK), "connection_failed"),
    ],
)
def test_network_exceptions_never_expose_callback_details(wire, error, reason):
    connection, _response, _factory = wire
    connection.request.side_effect = error
    exc = assert_reason(reason, delivery.post_signed, CALLBACK, SECRET, "sub_1", "evt_1", b"{}")
    assert exc.__suppress_context__ is True
    connection.close.assert_called_once()


def test_total_request_deadline_interrupts_slow_headers(monkeypatch):
    monkeypatch.setattr(delivery, "CALLBACK_TIMEOUT_SECONDS", 0.03)
    connection = MagicMock()
    connection.deadline = time.monotonic() + 0.03
    connection.timed_out = threading.Event()
    connection.abort_timeout.side_effect = connection.timed_out.set

    def wait_for_abort():
        assert connection.timed_out.wait(timeout=2)
        raise OSError(CALLBACK)

    connection.getresponse.side_effect = wait_for_abort
    monkeypatch.setattr(delivery, "_PinnedHTTPSConnection", MagicMock(return_value=connection))
    assert_reason("timeout", delivery.post_signed, CALLBACK, SECRET, "sub_1", "evt_1", b"{}")
    connection.close.assert_called_once()


def test_verification_challenge_is_fresh_signed_and_constant_time(monkeypatch):
    seen = []
    compare = MagicMock(wraps=hmac.compare_digest)
    monkeypatch.setattr(delivery.hmac, "compare_digest", compare)

    def accept(url, secret, subscription_id, event_id, body):
        assert (url, secret, subscription_id) == (CALLBACK, SECRET, "sub_1")
        value = json.loads(body)
        assert value["type"] == "verification"
        assert event_id.startswith("msg_verification_")
        assert len(value["challenge"]) >= 32
        seen.append((event_id, value["challenge"]))
        return delivery.CallbackResponse(200, json.dumps({"challenge": value["challenge"]}).encode())

    monkeypatch.setattr(delivery, "post_signed", accept)
    delivery.verify_callback(CALLBACK, SECRET, "sub_1")
    delivery.verify_callback(CALLBACK, SECRET, "sub_1")
    assert seen[0][0] != seen[1][0]
    assert seen[0][1] != seen[1][1]
    assert compare.call_count == 2
    assert compare.call_args.args == (seen[1][1].encode(), seen[1][1].encode())


@pytest.mark.parametrize(
    "status,body",
    [
        (400, b'{"challenge":"wrong"}'),
        (202, b'{"challenge":"wrong"}'),
        (200, b"not json"),
        (200, b"[]"),
        (200, b"null"),
        (200, b'{"challenge":true}'),
        (200, b'{"challenge":null}'),
        (200, b'{"challenge":"\\ud800"}'),
        (200, b"{}"),
    ],
)
def test_verification_requires_2xx_and_exact_string_echo(monkeypatch, status, body):
    monkeypatch.setattr(delivery, "post_signed", MagicMock(return_value=delivery.CallbackResponse(status, body)))
    assert_reason("challenge_failed", delivery.verify_callback, CALLBACK, SECRET, "sub_1")


def test_unsubscribe_can_normalize_previously_allowed_url_without_egress(settings):
    settings.MCP_EVENTS_ALLOWED_CALLBACK_HOSTS = []
    assert delivery.validate_callback_url(CALLBACK, check_allowlist=False) == CALLBACK
    assert_reason("callback_host_not_allowed", delivery.post_signed, CALLBACK, SECRET, "sub_1", "evt_1", b"{}")
    assert_reason(
        "invalid_callback_url", delivery.validate_callback_url, "http://receiver.example.com/", check_allowlist=False
    )
    assert_reason("invalid_callback_url", delivery.validate_callback_url, "https://127.0.0.1/", check_allowlist=False)
    delivery.socket.getaddrinfo.assert_not_called()
    delivery.socket.socket.assert_not_called()


@pytest.mark.parametrize("status", [410, 413])
def test_permanent_status_does_not_wait_for_untrusted_error_body(wire, status):
    _connection, response, _factory = wire
    response.status = status
    response.read1.side_effect = TimeoutError("synthetic stalled error response")
    result = delivery.post_signed(CALLBACK, SECRET, "sub_1", "evt_1", b"{}")
    assert result == delivery.CallbackResponse(status, b"")
    response.read1.assert_not_called()
    response.close.assert_called_once()


def test_http_wire_keeps_original_host_and_does_not_use_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setattr(delivery.socket, "getaddrinfo", MagicMock(return_value=[dns_address("1.1.1.1")]))
    raw_socket = MagicMock()
    raw_socket.makefile.return_value = io.BytesIO(
        b"HTTP/1.1 202 Accepted\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}"
    )
    monkeypatch.setattr(delivery.socket, "socket", MagicMock(return_value=raw_socket))
    context = MagicMock()
    context.wrap_socket.return_value = raw_socket
    monkeypatch.setattr(delivery.ssl, "create_default_context", MagicMock(return_value=context))
    result = delivery.post_signed(CALLBACK, SECRET, "sub_1", "evt_1", b'{"data":{}}')
    assert result == delivery.CallbackResponse(202, b"{}")
    raw_socket.connect.assert_called_once_with(("1.1.1.1", 443))
    context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="receiver.example.com")
    request = b"".join(call.args[0] for call in raw_socket.sendall.call_args_list)
    assert request.startswith(b"POST /hooks/events?token=synthetic-test-only HTTP/1.1\r\n")
    assert b"Host: receiver.example.com\r\n" in request
    assert b"Host: 1.1.1.1" not in request
    assert request.endswith(b'{"data":{}}')


def test_connection_tries_only_already_validated_public_alternatives(monkeypatch):
    monkeypatch.setattr(
        delivery.socket, "getaddrinfo", MagicMock(return_value=[dns_address("1.1.1.1"), dns_address("8.8.8.8")])
    )
    first, second = MagicMock(), MagicMock()
    first.connect.side_effect = OSError("synthetic connection failure")
    monkeypatch.setattr(delivery.socket, "socket", MagicMock(side_effect=[first, second]))
    connection = delivery._PinnedHTTPSConnection("receiver.example.com", deadline=time.monotonic() + 10)
    monkeypatch.setattr(connection._context, "wrap_socket", MagicMock(return_value=second))
    connection.connect()
    first.connect.assert_called_once_with(("1.1.1.1", 443))
    first.close.assert_called_once()
    second.connect.assert_called_once_with(("8.8.8.8", 443))
    assert connection.sock is second


@pytest.mark.parametrize(
    "error,reason", [(TimeoutError(CALLBACK), "timeout"), (OSError(CALLBACK), "connection_failed")]
)
def test_real_connection_class_closes_failed_socket_and_sanitizes_error(monkeypatch, error, reason):
    monkeypatch.setattr(delivery.socket, "getaddrinfo", MagicMock(return_value=[dns_address("1.1.1.1")]))
    raw_socket = MagicMock()
    raw_socket.connect.side_effect = error
    monkeypatch.setattr(delivery.socket, "socket", MagicMock(return_value=raw_socket))
    assert_reason(reason, delivery.post_signed, CALLBACK, SECRET, "sub_1", "evt_1", b"{}")
    raw_socket.close.assert_called_once()


def test_tls_verification_failure_never_tries_another_address(monkeypatch):
    monkeypatch.setattr(
        delivery.socket, "getaddrinfo", MagicMock(return_value=[dns_address("1.1.1.1"), dns_address("8.8.8.8")])
    )
    raw_socket = MagicMock()
    socket_factory = MagicMock(return_value=raw_socket)
    monkeypatch.setattr(delivery.socket, "socket", socket_factory)
    context = MagicMock()
    context.wrap_socket.side_effect = ssl.SSLCertVerificationError(CALLBACK)
    monkeypatch.setattr(delivery.ssl, "create_default_context", MagicMock(return_value=context))
    assert_reason("tls_failed", delivery.post_signed, CALLBACK, SECRET, "sub_1", "evt_1", b"{}")
    socket_factory.assert_called_once()
    raw_socket.close.assert_called_once()


def test_expired_connection_deadline_closes_a_just_completed_tls_socket(monkeypatch):
    monkeypatch.setattr(delivery.socket, "getaddrinfo", MagicMock(return_value=[dns_address("1.1.1.1")]))
    raw_socket, tls_socket = MagicMock(), MagicMock()
    monkeypatch.setattr(delivery.socket, "socket", MagicMock(return_value=raw_socket))
    connection = delivery._PinnedHTTPSConnection("receiver.example.com", deadline=time.monotonic() + 10)

    def late_handshake(*args, **kwargs):
        connection.deadline = time.monotonic() - 1
        return tls_socket

    monkeypatch.setattr(connection._context, "wrap_socket", late_handshake)
    assert_reason("timeout", connection.connect)
    tls_socket.close.assert_called_once()
    assert connection.sock is None


def test_dns_capacity_exhaustion_is_bounded_without_adding_work(monkeypatch):
    slots = MagicMock()
    slots.acquire.return_value = False
    monkeypatch.setattr(delivery, "_DNS_SLOTS", slots)
    assert_reason("timeout", delivery._resolve_public_addresses, "receiver.example.com", time.monotonic() + 1)
    delivery.socket.getaddrinfo.assert_not_called()


def test_dns_executor_failure_releases_reserved_slot(monkeypatch):
    slots, pool = MagicMock(), MagicMock()
    pool.submit.side_effect = RuntimeError("synthetic executor shutdown")
    monkeypatch.setattr(delivery, "_DNS_SLOTS", slots)
    monkeypatch.setattr(delivery, "_DNS_POOL", pool)
    assert_reason("dns_failed", delivery._resolve_public_addresses, "receiver.example.com", time.monotonic() + 1)
    slots.release.assert_called_once()
