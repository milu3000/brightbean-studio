"""Allowlisted DM error diagnostics, never a source of delivery/retry evidence.

Do not log exceptions, request/response bodies, provider messages, trace IDs,
credentials, account IDs, or conversation content here. Numeric error fields
help investigate a refusal without retaining those sensitive values. Categories
are deliberately broad: a Meta permission-shaped error alone cannot prove a
messaging-window failure or that reconnecting will fix it.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from typing import TypedDict
from uuid import UUID

from providers.exceptions import OAuthError, RateLimitError, TokenExpiredError

logger = logging.getLogger(__name__)


class ProviderFailureDiagnostics(TypedDict):
    http_status: int | None
    code: int | None
    subcode: int | None
    category: str


def _numeric_code(value: object) -> int | None:
    # Reject bool, strings, containers and unbounded values rather than coercing
    # potentially private provider-controlled content into a log field.
    return value if type(value) is int and 0 <= value <= 2**31 - 1 else None


def provider_failure_diagnostics(exc: Exception) -> ProviderFailureDiagnostics:
    status = _numeric_code(getattr(exc, "status_code", None))
    if status is not None and not 100 <= status <= 599:
        status = None
    raw = getattr(exc, "raw_response", None)
    error = raw.get("error") if isinstance(raw, dict) else None
    code = _numeric_code(error.get("code")) if isinstance(error, dict) else None
    subcode = _numeric_code(error.get("error_subcode")) if isinstance(error, dict) else None
    meta = getattr(exc, "platform", None) in {"Facebook", "Instagram (Direct)"}
    if isinstance(exc, TokenExpiredError):
        category = "authentication_expired"
    elif isinstance(exc, OAuthError) or status == 401 or (meta and code == 190):
        category = "authentication"
    elif isinstance(exc, RateLimitError) or status == 429 or (meta and code in {4, 17, 32, 613}):
        category = "rate_limit"
    elif status is not None and status >= 500:
        category = "temporary"
    elif status == 403 or (meta and code in {10, 200}):
        category = "permission_or_policy"
    elif status is not None and status >= 400:
        category = "request_rejected"
    else:
        category = "unknown"
    return {"http_status": status, "code": code, "subcode": subcode, "category": category}


def log_dm_provider_failure(reply_id: UUID, exc: Exception) -> None:
    # Logging is best-effort and must never change the committed receipt. The
    # existing internal reply UUID provides correlation without a recipient ID.
    with suppress(Exception):
        diagnostics = provider_failure_diagnostics(exc)
        logger.warning("DM provider failure %s", json.dumps({"reply_id": str(reply_id), **diagnostics}, sort_keys=True))


def provider_failure_reason(exc: Exception) -> str:
    category = provider_failure_diagnostics(exc)["category"]
    return {
        "authentication_expired": "the connection has expired. Reconnect the account in Workspace Settings.",
        "authentication": (
            "the account's authorization was rejected. Check its connection in Workspace Settings before trying again."
        ),
        "permission_or_policy": (
            "the platform refused this reply under its permissions or messaging rules. "
            "Check those requirements before trying again."
        ),
        "rate_limit": "the account has hit its rate limit. Wait a few minutes and try again.",
        "temporary": "the platform reported a temporary error. Check delivery status before trying again.",
        "request_rejected": "the platform rejected the request. Review the reply and account requirements before trying again.",
        "unknown": "the reply could not be completed. Review its delivery status and error details before trying again.",
    }[category]
