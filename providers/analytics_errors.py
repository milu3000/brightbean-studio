"""Conservative, side-effect-free classification of analytics failures.

An object-level refusal must not turn a healthy account into a reconnect
warning. In particular, Meta's unsupported-object error lists *possible*
causes, including permissions; it proves neither a missing scope nor deletion.
Only typed errors and specific provider codes (or named missing scopes) can
escalate a post failure to an account-wide problem.

The result deliberately retains no exception, message, URL or response body.
Its evidence can be stored or logged without copying tokens from API errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import httpx

from .exceptions import OAuthError, RateLimitError, TokenExpiredError
from .google_errors import AUTH_REASONS, QUOTA_REASONS, THROTTLE_REASONS

AnalyticsErrorCategory = Literal[
    "account_auth", "account_scope", "post_inaccessible", "post_archived", "post_deleted", "transient", "unknown"
]
AnalyticsErrorContext = Literal["post", "account"]

_META_PLATFORMS = frozenset({"threads", "instagram", "instagram_login", "facebook"})
_GOOGLE_PLATFORMS = frozenset({"youtube", "google_business"})
_OAUTH_AUTH_CODES = frozenset({"invalid_grant", "invalid_token"})
_TIKTOK_SCOPE_CODES = frozenset({"scope_not_authorized", "scope_permission_missed"})
_TIKTOK_TRANSIENT_CODES = frozenset({"rate_limit_exceeded", "internal_error"})
_GOOGLE_SCOPE_REASONS = frozenset({"insufficientpermissions", "access_token_scope_insufficient"})
_GOOGLE_TRANSIENT_REASONS = QUOTA_REASONS | THROTTLE_REASONS | {"internalerror", "backenderror", "unavailable"}
_GOOGLE_POST_REASONS = frozenset({"videonotfound", "notfound", "forbidden"})

# Keep the string evidence vocabulary closed. Merely looking like an API code
# is not enough: a token can also be an arbitrarily long alphanumeric string.
_SAFE_STRING_CODES = (
    _OAUTH_AUTH_CODES | _TIKTOK_SCOPE_CODES | _TIKTOK_TRANSIENT_CODES | {"access_token_invalid", "invalid_params"}
)
_SAFE_REASONS = AUTH_REASONS | _GOOGLE_SCOPE_REASONS | _GOOGLE_TRANSIENT_REASONS | _GOOGLE_POST_REASONS

_ANALYTICS_SCOPES = {
    "threads": frozenset({"threads_manage_insights"}),
    "instagram": frozenset({"instagram_manage_insights"}),
    "instagram_login": frozenset({"instagram_business_manage_insights"}),
    "facebook": frozenset({"read_insights"}),
    "youtube": frozenset({"https://www.googleapis.com/auth/yt-analytics.readonly"}),
    "tiktok": frozenset({"video.list"}),
}


@dataclass(frozen=True)
class AnalyticsErrorClassification:
    """Immutable classification containing only allowlisted scalar evidence."""

    category: AnalyticsErrorCategory
    http_status: int | None = None
    code: int | str | None = None
    subcode: int | None = None
    reason: str | None = None
    signal: str | None = None

    @property
    def is_account_error(self) -> bool:
        return self.category in {"account_auth", "account_scope"}

    @property
    def safe_evidence(self) -> dict[str, int | str]:
        """A fresh JSON-safe copy; mutating it cannot change this result."""
        values = {
            "http_status": self.http_status,
            "code": self.code,
            "subcode": self.subcode,
            "reason": self.reason,
            "signal": self.signal,
        }
        return {key: value for key, value in values.items() if value is not None}


def _numeric_code(value: object) -> int | None:
    # Reject booleans, floats, objects, long numeric secrets and arbitrary text.
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
        value = int(value)
    if type(value) is int and 0 <= value < 1_000_000_000:
        return value
    return None


def _safe_code(value: object) -> int | str | None:
    numeric = _numeric_code(value)
    if numeric is not None:
        return numeric
    if isinstance(value, str) and value.lower() in _SAFE_STRING_CODES:
        return value.lower()
    return None


def _error_envelope(exc: Exception) -> tuple[dict, dict]:
    body = getattr(exc, "raw_response", None)
    if not isinstance(body, dict):
        return {}, {}
    error = body.get("error")
    return body, error if isinstance(error, dict) else body


def _google_reasons(error: dict) -> set[str]:
    """Read known reason/status slots, not arbitrary nested response values."""
    reasons = set()
    entries = error.get("errors")
    if isinstance(entries, list):
        for entry in entries:
            value = entry.get("reason") if isinstance(entry, dict) else None
            if isinstance(value, str) and value.lower() in _SAFE_REASONS:
                reasons.add(value.lower())
    for key in ("reason", "status"):
        value = error.get(key)
        if isinstance(value, str) and value.lower() in _SAFE_REASONS:
            reasons.add(value.lower())
    # Newer Google APIs use google.rpc.ErrorInfo rather than errors[].reason.
    details = error.get("details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict) or detail.get("@type") != "type.googleapis.com/google.rpc.ErrorInfo":
                continue
            value = detail.get("reason")
            if isinstance(value, str) and value.lower() in _SAFE_REASONS:
                reasons.add(value.lower())
    return reasons


def _has_named_missing_scope(body: dict, error: dict, platform: str) -> bool:
    required = _ANALYTICS_SCOPES.get(platform, frozenset())
    if not required:
        return False
    for source in (body, error):
        for key in ("missing_scopes", "missing_permissions"):
            missing = source.get(key)
            if isinstance(missing, str):
                missing = [missing]
            if isinstance(missing, (list, tuple)) and any(
                isinstance(scope, str) and scope in required for scope in missing
            ):
                return True

    # Meta sometimes provides only a code plus a human message naming the
    # actual required scope. Accept narrow affirmative forms only, never the
    # words "permission", "scope" or "forbidden" by themselves. Do not inspect
    # str(exc), which can contain unrelated URLs or serialized user content.
    message = error.get("message")
    if platform not in _META_PLATFORMS or not isinstance(message, str):
        return False
    if _numeric_code(error.get("code")) not in {10, 200}:
        return False
    message = message.lower()
    for scope in required:
        name = re.escape(scope)
        boundary = r"(?:^|[.!?:]\s*|\(#[0-9]+\)\s*)"
        if re.search(
            boundary + rf"(?:requires?|missing)\s+(?:the\s+)?(?:permission\s+)?['\"`]?{name}['\"`]?(?![a-z0-9_.])",
            message,
        ):
            return True
        if re.search(
            boundary + rf"(?:the\s+)?['\"`]?{name}['\"`]?(?:\s+permission|\s+scope)?\s+is required\b", message
        ):
            return True
    return False


def classify_analytics_error(
    exc: Exception, platform: str, *, context: AnalyticsErrorContext = "post"
) -> AnalyticsErrorClassification:
    """Classify without mutating the error, doing IO, or inspecting account state.

    ``context`` is the target of the failed API call, not the enclosing job:
    a post fetch inside account backfill is still ``post``. Invalid contexts
    raise rather than silently promoting a post refusal to account scope.

    Providers may set ``exc.analytics_post_state`` to ``archived`` or
    ``deleted`` only after obtaining authoritative post-state evidence. That
    internal normalized signal is the sole source of those precise categories;
    404, 410, missing objects, and error-message prose are not proof of either.
    """
    if context not in {"post", "account"}:
        raise ValueError("Analytics error context must be 'post' or 'account'")
    platform = platform.strip().lower() if isinstance(platform, str) else ""
    platform = {"instagram (direct)": "instagram_login", "google business profile": "google_business"}.get(
        platform, platform
    )
    body, error = _error_envelope(exc)
    code = _safe_code(error.get("code"))
    if code is None and isinstance(body.get("error"), str):
        code = _safe_code(body["error"])
    subcode = _numeric_code(error.get("error_subcode"))
    status = _numeric_code(getattr(exc, "status_code", None))
    if status is not None and not 100 <= status <= 599:
        status = None
    reasons = _google_reasons(error) if platform in _GOOGLE_PLATFORMS else set()

    def result(
        category: AnalyticsErrorCategory, signal: str, *, reason: str | None = None
    ) -> AnalyticsErrorClassification:
        return AnalyticsErrorClassification(category, status, code, subcode, reason, signal)

    if isinstance(exc, TokenExpiredError):
        return result("account_auth", "token_expired")
    if isinstance(exc, RateLimitError):
        return result("transient", "rate_limit")
    if platform in _META_PLATFORMS and code in {102, 190}:
        return result("account_auth", "meta_auth")
    if code in _OAUTH_AUTH_CODES:
        return result("account_auth", "oauth_auth")
    if isinstance(exc, OAuthError) and re.search(r"\binvalid_grant\b", str(exc), flags=re.IGNORECASE):
        # Some refresh adapters retain the OAuth error name only in this typed
        # exception's message. Keep this exact-code fallback, never its text.
        return result("account_auth", "oauth_invalid_grant")
    if platform == "tiktok" and code == "access_token_invalid":
        return result("account_auth", "tiktok_auth")
    if reasons & AUTH_REASONS:
        return result("account_auth", "google_auth", reason=sorted(reasons & AUTH_REASONS)[0])

    if reasons & _GOOGLE_TRANSIENT_REASONS:
        return result("transient", "google_transient", reason=sorted(reasons & _GOOGLE_TRANSIENT_REASONS)[0])
    if platform == "tiktok" and code in _TIKTOK_TRANSIENT_CODES:
        return result("transient", "tiktok_transient")
    if platform in _META_PLATFORMS and (code in {1, 2, 4, 17, 32, 341, 613} or error.get("is_transient") is True):
        return result("transient", "meta_transient")

    if platform == "tiktok" and code in _TIKTOK_SCOPE_CODES:
        # TikTok uses 401 for scope_not_authorized, so inspect its code before
        # the generic 401 fallback or it would be mislabelled token expiry.
        return result("account_scope", "tiktok_scope")
    if reasons & _GOOGLE_SCOPE_REASONS:
        return result("account_scope", "google_scope", reason=sorted(reasons & _GOOGLE_SCOPE_REASONS)[0])
    if _has_named_missing_scope(body, error, platform):
        return result("account_scope", "required_analytics_scope")
    if status == 401:
        return result("account_auth", "http_unauthorized")
    if status == 429 or (status is not None and 500 <= status <= 599):
        return result("transient", "http_transient")
    if isinstance(
        exc, (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
    ):
        return result("transient", "network_error")

    if context == "post":
        state = getattr(exc, "analytics_post_state", None)
        if state == "archived":
            return result("post_archived", "provider_post_archived")
        if state == "deleted":
            return result("post_deleted", "provider_post_deleted")

    if platform in _META_PLATFORMS:
        if code in {10, 200}:
            return result("account_scope" if context == "account" else "post_inaccessible", "meta_permission")
        message = error.get("message")
        unsupported_object = code == 100 and (
            subcode == 33 or (isinstance(message, str) and message.lower().startswith("unsupported get request"))
        )
        if unsupported_object:
            return result("post_inaccessible" if context == "post" else "unknown", "meta_object_unavailable")

    if context == "post":
        if reasons & _GOOGLE_POST_REASONS:
            return result(
                "post_inaccessible", "google_object_unavailable", reason=sorted(reasons & _GOOGLE_POST_REASONS)[0]
            )
        if status in {404, 410}:
            return result("post_inaccessible", "http_object_unavailable")

    return result("unknown", "unclassified")
