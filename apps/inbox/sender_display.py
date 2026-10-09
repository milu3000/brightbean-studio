"""Truthful, read-only sender labels shared by legacy and canonical inboxes.

This is presentation, never routing or identity proof. A legacy ``sender_handle``
on Meta is frequently a scoped user ID; it must not become an invented @handle.
No other message, peer, body, timestamp or external profile is consulted here.
"""

import re
from collections.abc import Mapping

_META_PLATFORMS = frozenset({"facebook", "instagram", "instagram_login"})
_PLACEHOLDERS = frozenset({"unknown", "unknown sender", "sender unavailable"})


def _text(value):
    if not isinstance(value, str) or len(value) > 255:
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    return value.strip()


def _native_id(value):
    # IDs are opaque. Never trim or coerce one into a different identity.
    if not isinstance(value, str) or not value or len(value) > 255:
        return ""
    return "" if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) else value


def _name(value, native_ids):
    value = _text(value)
    if value.casefold() in _PLACEHOLDERS or value.removeprefix("@").isdigit():
        return ""
    return "" if value.removeprefix("@") in native_ids else value


def _username(value, native_ids):
    value = _name(value, native_ids).removeprefix("@")
    return value if re.fullmatch(r"[\w.-]+", value) else ""


def normalize_sender_name(sender):
    """Use supplied name, then supplied username, never stringify raw objects/IDs."""
    if not isinstance(sender, Mapping):
        return ""
    native_ids = {_native_id(sender.get("id"))} - {""}
    return _name(sender.get("name"), native_ids) or _username(sender.get("username"), native_ids)


def sender_display(record, *, platform=""):
    """Return a bounded label/name/handle/native_id without changing the record.

    Pass the account's platform for a legacy model (which has no platform field).
    A DTO may carry its platform and explicit sender_username/sender_native_id.
    Only an explicitly recorded username can give a Meta sender an @handle.
    """

    def field(key):
        return record.get(key) if isinstance(record, Mapping) else getattr(record, key, None)

    platform = platform or field("platform") or ""
    extra = field("extra")
    extra = extra if isinstance(extra, Mapping) else {}
    raw_sender = extra.get("sender")
    raw_sender = raw_sender if isinstance(raw_sender, Mapping) else {}
    ids = [_native_id(field("sender_id")), _native_id(field("sender_native_id"))]
    legacy_handle = _text(field("sender_handle"))
    explicit_username = _text(field("sender_username")).removeprefix("@")
    if (
        platform in _META_PLATFORMS
        and legacy_handle.removeprefix("@") != explicit_username
        or legacy_handle.removeprefix("@").isdigit()
    ) and not legacy_handle.startswith("@"):
        ids.append(_native_id(field("sender_handle")))
    ids.extend(
        _native_id(value) for value in (extra.get("message_sender_id"), extra.get("sender_id"), raw_sender.get("id"))
    )
    native_ids = set(ids) - {""}
    native_id = next((value for value in ids if value), "")

    # Do not use an embedded sender's attributes when it names a different ID.
    embedded_matches = len(native_ids) == 1 and _native_id(raw_sender.get("id")) == native_id
    username = _username(field("sender_username"), native_ids)
    if not username and embedded_matches:
        username = _username(raw_sender.get("username"), native_ids)
    if not username and platform not in _META_PLATFORMS:
        username = _username(legacy_handle, native_ids)
    name = _name(field("sender_name"), native_ids)
    if not name and embedded_matches:
        name = _name(raw_sender.get("name"), native_ids)
    return {
        "label": name or (f"@{username}" if username else "Unknown sender"),
        "name": name,
        "handle": username,
        "native_id": native_id,
    }
