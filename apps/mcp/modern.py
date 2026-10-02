"""MCP 2026-07-28 per-request wire contract, alongside legacy MCP.

No negotiation state is kept: identity/permissions always come from the bearer
credential, never the untrusted client metadata. Reference: the dated MCP basic,
versioning, Streamable HTTP, discovery and caching specifications.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpRequest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from apps.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    MCP_PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    JsonRpcError,
    make_error,
)

MODERN_PROTOCOL_VERSION = "2026-07-28"
META_PREFIX = "io.modelcontextprotocol/"
VERSION_KEY = META_PREFIX + "protocolVersion"
CAPABILITIES_KEY = META_PREFIX + "clientCapabilities"
SERVER_INFO_KEY = META_PREFIX + "serverInfo"
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022


# Vendored, dated upstream definitions only; all references are local. Never
# fetch schemas or dereference a client-supplied URL during validation.
_PROTOCOL_SCHEMA = json.loads((Path(__file__).parent / "schemas" / "mcp-2026-07-28.json").read_text())
_META_VALIDATOR = Draft202012Validator(
    {**_PROTOCOL_SCHEMA, "$ref": "#/$defs/RequestMetaObject"}, format_checker=FormatChecker()
)


def modern_requested(body, request: HttpRequest) -> bool:
    """Recognize modern intent, including incomplete metadata (fail closed)."""
    if isinstance(body, list):
        header = request.headers.get("MCP-Protocol-Version")
        return (
            header is not None
            and header != MCP_PROTOCOL_VERSION
            or any(modern_requested(item, request) for item in body)
        )
    params = body.get("params") if isinstance(body, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    has_protocol_meta = isinstance(meta, dict) and any(key.startswith(META_PREFIX) for key in meta)
    if isinstance(body, dict) and body.get("method") == "initialize" and not has_protocol_meta:
        # The dual-era handshake selects legacy behavior, including clients
        # proposing a different version. Always negotiate our legacy revision.
        return False
    header = request.headers.get("MCP-Protocol-Version")
    if header is not None and header != MCP_PROTOCOL_VERSION:
        return True
    if not isinstance(body, dict):
        return False
    return (
        body.get("method") == "server/discover"
        or str(body.get("method", "")).startswith("events/")
        or has_protocol_meta
    )


def _origin(value):
    if not isinstance(value, str) or not value or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, TypeError):
        return None


def origin_allowed(request: HttpRequest) -> bool:
    """Validate present browser origins on both eras; never accept a wildcard."""
    supplied = request.headers.get("Origin")
    if supplied is None:
        return True
    origin = _origin(supplied)
    if origin is None:
        return False
    allowed = [f"{request.scheme}://{request.get_host()}"]
    allowed.extend(getattr(settings, "MCP_ALLOWED_ORIGINS", []))
    return any(origin == _origin(item) for item in allowed)


def _header(value, *, encoded=False):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise JsonRpcError(HEADER_MISMATCH, "Required MCP header is missing or malformed")
    if any(not (0x20 <= ord(char) <= 0x7E or char == "\t") for char in value):
        raise JsonRpcError(HEADER_MISMATCH, "MCP header contains invalid characters")
    if encoded and value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            raise JsonRpcError(HEADER_MISMATCH, "MCP header encoding is invalid") from None
    return value


def validate_request(body, request: HttpRequest) -> None:
    """Validate before any handler, mutation, callback, or tool can execute."""
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        raise JsonRpcError(INVALID_REQUEST, "Expected a single JSON-RPC request or notification")
    if "result" in body or "error" in body:
        raise JsonRpcError(INVALID_REQUEST, "Client responses are not accepted")
    if "id" in body and (isinstance(body["id"], bool) or not isinstance(body["id"], (str, int))):
        raise JsonRpcError(INVALID_REQUEST, "Request id must be a string or integer")
    params = body.get("params", {})
    if not isinstance(params, dict):
        raise JsonRpcError(INVALID_PARAMS, "params must be an object")
    meta = params.get("_meta")
    # Standard notifications have no RequestMetaObject requirement. They still
    # carry matching transport headers, and cannot invoke request-only methods.
    notification = "id" not in body
    if notification and not body["method"].startswith("notifications/"):
        raise JsonRpcError(INVALID_REQUEST, "This method requires a request id")
    if not notification or meta is not None:
        try:
            _META_VALIDATOR.validate(meta)
        except (ValidationError, RecursionError) as exc:
            # Do not echo metadata values, which could contain private data.
            raise JsonRpcError(INVALID_PARAMS, "Required request metadata is missing or malformed") from exc
    version = _header(request.headers.get("MCP-Protocol-Version"))
    method = _header(request.headers.get("Mcp-Method"))
    if method != body["method"] or meta is not None and version != meta[VERSION_KEY]:
        raise JsonRpcError(HEADER_MISMATCH, "MCP headers do not match the request body")
    if version != MODERN_PROTOCOL_VERSION:
        raise JsonRpcError(
            UNSUPPORTED_VERSION,
            "Unsupported protocol version",
            {"supported": [MODERN_PROTOCOL_VERSION, MCP_PROTOCOL_VERSION], "requested": version},
        )
    name_field = "uri" if method == "resources/read" else "name"
    if method in {"tools/call", "resources/read", "prompts/get"} and _header(
        request.headers.get("Mcp-Name"), encoded=True
    ) != params.get(name_field):
        raise JsonRpcError(HEADER_MISMATCH, "Mcp-Name header does not match the request body")
    # We advertise no x-mcp-header parameters and never use client headers for
    # authentication, permission selection or account/workspace routing.


def complete_result(result: dict, method: str) -> dict:
    result = {**result, "resultType": "complete"}
    result["_meta"] = {**result.get("_meta", {}), SERVER_INFO_KEY: {"name": SERVER_NAME, "version": SERVER_VERSION}}
    if method in {"server/discover", "tools/list"}:
        # Avoid stale permission/config snapshots or cache sharing across users.
        result.update(ttlMs=0, cacheScope="private")
    return result


def error_response(body, code, message, data=None):
    """Modern error envelopes omit an unavailable/invalid request identifier."""
    message_id = body.get("id") if isinstance(body, dict) else None
    response = make_error(message_id, code, message, data)
    if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
        response.pop("id")
    return response
