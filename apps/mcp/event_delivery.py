"""Fail-closed, signed outbound HTTPS transport for MCP Events.

Only operator-allowlisted DNS hosts are eligible. Each connection resolves all
addresses afresh, rejects the entire answer if any address is not public, and
opens the socket to a validated numeric address. TLS and the HTTP Host header
still use the original DNS name. Proxies and redirects are never used.

The payload is serialized once; retries call ``post_signed`` again with those
same bytes and event ID to obtain a fresh signing timestamp and signature.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import json
import re
import secrets
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from http.client import HTTPException, HTTPSConnection
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings

MAX_PAYLOAD_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
CALLBACK_TIMEOUT_SECONDS = 10.0
MAX_CALLBACK_URL_LENGTH = 4096

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*\Z")
_LOCAL_SUFFIXES = ("localhost", "local", "localdomain", "internal", "lan", "home", "home.arpa")
# Bound simultaneous blocking libc DNS lookups as well as the caller's wait.
# A slow resolver occupies a slot until it finishes, rather than growing a queue.
_DNS_SLOTS = threading.BoundedSemaphore(4)
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp-callback-dns")
_ERROR_REASONS = frozenset(
    {
        "invalid_callback_url",
        "callback_host_not_allowed",
        "unsafe_address",
        "dns_failed",
        "timeout",
        "tls_failed",
        "connection_failed",
        "redirect_blocked",
        "response_too_large",
        "challenge_failed",
        "invalid_secret",
        "invalid_payload",
        "payload_too_large",
        "invalid_identifier",
    }
)


class CallbackError(Exception):
    """Safe category and message; never expose a URL, secret, or response body."""

    def __init__(self, reason: str):
        self.reason = reason if reason in _ERROR_REASONS else "connection_failed"
        super().__init__("Callback request failed.")


@dataclass(frozen=True)
class CallbackResponse:
    status: int
    body: bytes = field(repr=False)


def _validate_host(host: str) -> None:
    if (
        not host
        or len(host) > 253
        or "." not in host
        or not all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
        or _NUMERIC_HOST.fullmatch(host)
        or any(host == suffix or host.endswith("." + suffix) for suffix in _LOCAL_SUFFIXES)
    ):
        raise CallbackError("invalid_callback_url")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    raise CallbackError("invalid_callback_url")


def _require_allowed_host(host: str) -> None:
    configured = getattr(settings, "MCP_EVENTS_ALLOWED_CALLBACK_HOSTS", ())
    if isinstance(configured, str):
        configured = configured.split(",")
    if not isinstance(configured, (list, tuple, set, frozenset)):
        raise CallbackError("callback_host_not_allowed")
    # Exact DNS names only: no suffix matching, wildcards, ports or URL entries.
    allowed = {item.strip().lower() for item in configured if isinstance(item, str)}
    if host not in allowed:
        raise CallbackError("callback_host_not_allowed")


def validate_callback_url(url: str, *, check_allowlist: bool = True) -> str:
    """Normalize HTTPS syntax. Skip the allowlist only for local unsubscribe.

    This never resolves or transmits data. ``post_signed`` and the connection
    independently enforce the current allowlist, even for an existing URL.
    """
    if (
        not isinstance(url, str)
        or not url
        or len(url) > MAX_CALLBACK_URL_LENGTH
        or not url.isascii()
        or any(ord(char) <= 32 or ord(char) == 127 for char in url)
        or "\\" in url
        or "#" in url
    ):
        raise CallbackError("invalid_callback_url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.netloc.lower() not in (host, host + ":443")
        ):
            raise CallbackError("invalid_callback_url")
    except ValueError:
        raise CallbackError("invalid_callback_url") from None
    _validate_host(host)
    if check_allowlist:
        _require_allowed_host(host)
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def validate_secret(secret: str) -> bytes:
    """Decode a canonical Standard Webhooks whsec_ key of 24–64 bytes."""
    if not isinstance(secret, str) or not secret.startswith("whsec_") or not 38 <= len(secret) <= 94:
        raise CallbackError("invalid_secret")
    encoded = secret[6:]
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise CallbackError("invalid_secret") from None
    if not 24 <= len(key) <= 64 or base64.b64encode(key).decode("ascii") != encoded:
        raise CallbackError("invalid_secret")
    return key


def canonical_json(value: Any) -> bytes:
    """Stable compact UTF-8 JSON; non-finite numbers are not JSON values."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CallbackError("invalid_payload") from None


def serialize_payload(value: dict) -> bytes:
    """Serialize an event once and enforce the receiver's 256 KiB limit."""
    body = canonical_json(value)
    if len(body) > MAX_PAYLOAD_BYTES:
        raise CallbackError("payload_too_large")
    return body


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CallbackError("timeout")
    return remaining


def _lookup(host: str) -> list:
    try:
        return socket.getaddrinfo(host, 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    finally:
        _DNS_SLOTS.release()


def _resolve_public_addresses(host: str, deadline: float) -> list:
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise CallbackError("timeout")
    try:
        future = _DNS_POOL.submit(_lookup, host)
    except RuntimeError:
        _DNS_SLOTS.release()
        raise CallbackError("dns_failed") from None
    try:
        results = future.result(timeout=_remaining(deadline))
    except TimeoutError:
        raise CallbackError("timeout") from None
    except OSError:
        raise CallbackError("dns_failed") from None
    if not results:
        raise CallbackError("dns_failed")

    for family, socktype, proto, _canonname, sockaddr in results:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            raise CallbackError("unsafe_address") from None
        if (
            family not in (socket.AF_INET, socket.AF_INET6)
            or socktype != socket.SOCK_STREAM
            or proto != socket.IPPROTO_TCP
            or sockaddr[1] != 443
            or "%" in sockaddr[0]
            or not address.is_global
            or address.is_reserved
            or address.is_multicast
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
        ):
            raise CallbackError("unsafe_address")
        if family == socket.AF_INET and address.version != 4:
            raise CallbackError("unsafe_address")
        if family == socket.AF_INET6 and (
            not isinstance(address, ipaddress.IPv6Address)
            or sockaddr[2:] != (0, 0)
            or address.ipv4_mapped is not None
            or address.sixtofour is not None
            or address.teredo is not None
            or address in ipaddress.IPv6Network("64:ff9b::/96")
            or address in ipaddress.IPv6Network("64:ff9b:1::/48")
        ):
            raise CallbackError("unsafe_address")
    # Validate the *entire* DNS answer before opening even the first socket.
    return results


class _PinnedHTTPSConnection(HTTPSConnection):
    """Numeric socket destination, original TLS SNI and certificate identity."""

    # Initialized by HTTPSConnection; omitted from typeshed's public interface.
    _context: ssl.SSLContext
    _tunnel_host: str | None

    def __init__(self, host: str, *, deadline: float):
        super().__init__(host, port=443, timeout=_remaining(deadline), context=ssl.create_default_context())
        self.deadline = deadline
        self.timed_out = threading.Event()
        self._transport_socket: socket.socket | None = None

    def abort_timeout(self) -> None:
        self.timed_out.set()
        sock = self._transport_socket
        if sock is not None:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()

    def connect(self) -> None:
        # No proxy tunnelling, caller-supplied context, or HTTPConnection's
        # create_connection: each could bypass destination or TLS validation.
        if self._tunnel_host is not None or self.port != 443:
            raise CallbackError("invalid_callback_url")
        _validate_host(self.host)
        _require_allowed_host(self.host)
        results = _resolve_public_addresses(self.host, self.deadline)
        for family, socktype, proto, _canonname, sockaddr in results:
            raw_socket = socket.socket(family, socktype, proto)
            self.sock = self._transport_socket = raw_socket
            try:
                raw_socket.settimeout(_remaining(self.deadline))
                raw_socket.connect(sockaddr)
                raw_socket.settimeout(_remaining(self.deadline))
                self.sock = self._transport_socket = self._context.wrap_socket(raw_socket, server_hostname=self.host)
                _remaining(self.deadline)
                return
            except (ssl.SSLError, TimeoutError):
                self.close()
                raise
            except OSError:
                self.close()
                _remaining(self.deadline)
            except CallbackError:
                self.close()
                raise
        raise CallbackError("connection_failed")


def _validate_identifier(value: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or any(not 33 <= ord(char) <= 126 for char in value):
        raise CallbackError("invalid_identifier")


def post_signed(
    url: str,
    secret: str,
    subscription_id: str,
    event_id: str,
    body: bytes,
    previous_secret: str = "",
) -> CallbackResponse:
    """Make one attempt. The caller handles HTTP retry policy, never redirects."""
    url = validate_callback_url(url)
    key = validate_secret(secret)
    old_key = validate_secret(previous_secret) if previous_secret else None
    _validate_identifier(subscription_id)
    _validate_identifier(event_id)
    if not isinstance(body, bytes):
        raise CallbackError("invalid_payload")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise CallbackError("payload_too_large")

    timestamp = str(int(time.time()))
    signed = event_id.encode("ascii") + b"." + timestamp.encode("ascii") + b"." + body
    keys = [key] + ([old_key] if old_key is not None and old_key != key else [])
    signatures = [
        "v1," + base64.b64encode(hmac.digest(signing_key, signed, "sha256")).decode("ascii") for signing_key in keys
    ]
    headers = {
        "Content-Type": "application/json",
        "webhook-id": event_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": " ".join(signatures),
        "X-MCP-Subscription-Id": subscription_id,
        "Connection": "close",
    }
    parsed = urlsplit(url)
    target = urlunsplit(("", "", parsed.path, parsed.query, ""))
    connection = _PinnedHTTPSConnection(parsed.hostname or "", deadline=time.monotonic() + CALLBACK_TIMEOUT_SECONDS)
    # A socket timeout alone restarts on every read and permits slow-drip HTTP
    # headers/body forever. Closing the socket enforces a whole-request bound.
    timer = threading.Timer(_remaining(connection.deadline), connection.abort_timeout)
    timer.daemon = True
    timer.start()
    response = None
    try:
        connection.request("POST", target, body=body, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise CallbackError("redirect_blocked")
        if response.status in (410, 413):
            # These statuses permanently stop a delivery. An oversized or
            # stalled error body must not turn that into a retryable failure.
            return CallbackResponse(status=response.status, body=b"")
        chunks = bytearray()
        while len(chunks) <= MAX_RESPONSE_BYTES:
            _remaining(connection.deadline)
            chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > MAX_RESPONSE_BYTES:
            raise CallbackError("response_too_large")
        _remaining(connection.deadline)
        return CallbackResponse(status=response.status, body=bytes(chunks))
    except CallbackError:
        raise
    except TimeoutError:
        raise CallbackError("timeout") from None
    except ssl.SSLError:
        reason = "timeout" if connection.timed_out.is_set() else "tls_failed"
        raise CallbackError(reason) from None
    except (OSError, HTTPException, ValueError):
        reason = "timeout" if connection.timed_out.is_set() else "connection_failed"
        raise CallbackError(reason) from None
    finally:
        timer.cancel()
        if response is not None:
            with suppress(OSError):
                response.close()
        with suppress(OSError):
            connection.close()


def verify_callback(url: str, secret: str, subscription_id: str) -> None:
    """Verify intent using one fresh, single-use signed challenge request."""
    challenge = secrets.token_urlsafe(32)
    response = post_signed(
        url,
        secret,
        subscription_id,
        "msg_verification_" + secrets.token_hex(16),
        serialize_payload({"type": "verification", "challenge": challenge}),
    )
    if not 200 <= response.status < 300:
        raise CallbackError("challenge_failed")
    try:
        value = json.loads(response.body)
    except (ValueError, UnicodeError, RecursionError):
        raise CallbackError("challenge_failed") from None
    echoed = value.get("challenge") if isinstance(value, dict) else None
    if not isinstance(echoed, str):
        raise CallbackError("challenge_failed")
    try:
        matched = hmac.compare_digest(echoed.encode("utf-8"), challenge.encode("utf-8"))
    except UnicodeError:
        raise CallbackError("challenge_failed") from None
    if not matched:
        raise CallbackError("challenge_failed")
