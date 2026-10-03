"""Fail-closed, exact account enrollment for the optional history rollout.

Enrollment is an operational gate, never authorization. Callers must still
check their actor's permissions and refresh persisted account ownership.
"""

import json
from uuid import UUID

from django.conf import settings

_FIELDS = {"workspace_id", "social_account_id", "platform"}
_PLATFORMS = {"instagram_login", "facebook"}


def enabled():
    return getattr(settings, "INBOX_CONVERSATION_V2_ENABLED", False) is True


def _identity(value):
    if (
        not isinstance(value, dict)
        or set(value) != _FIELDS
        or not isinstance(value.get("platform"), str)
        or value["platform"] not in _PLATFORMS
    ):
        return None
    try:
        # UUID objects are accepted for internal account snapshots; config
        # entries must contain strings (checked separately below).
        return (str(UUID(str(value["workspace_id"]))), str(UUID(str(value["social_account_id"]))), value["platform"])
    except (ValueError, TypeError, AttributeError):
        return None


def _enrollments(name):
    value = getattr(settings, name, [])
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError, RecursionError):
            return frozenset()
    if not isinstance(value, list):
        return frozenset()
    result = set()
    for entry in value:
        if not isinstance(entry, dict) or any(not isinstance(item, str) for item in entry.values()):
            return frozenset()
        identity = _identity(entry)
        if identity is None:
            return frozenset()
        result.add(identity)
    return frozenset(result)


def enrollment_identity(account):
    return _identity(
        {"workspace_id": account.workspace_id, "social_account_id": account.pk, "platform": account.platform}
    )


def capture_allowed(account):
    return enabled() and enrollment_identity(account) in _enrollments("INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS")


def read_allowed(account):
    return capture_allowed(account) and enrollment_identity(account) in _enrollments(
        "INBOX_CONVERSATION_V2_READ_ACCOUNTS"
    )


def read_available():
    return enabled() and bool(
        _enrollments("INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS") & _enrollments("INBOX_CONVERSATION_V2_READ_ACCOUNTS")
    )


def provider_options(account):
    """Bind one provider instance to a pinned identity, not a cached boolean."""
    identity = enrollment_identity(account)
    return (
        {"conversation_v2_scope": dict(zip(("workspace_id", "social_account_id", "platform"), identity, strict=True))}
        if identity
        else {}
    )


def provider_capture_allowed(options, *, platform):
    if not isinstance(options, dict):
        return False
    identity = _identity(options.get("conversation_v2_scope"))
    return bool(
        enabled()
        and identity
        and identity[2] == platform
        and identity in _enrollments("INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS")
    )
