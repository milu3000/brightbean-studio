"""Bounded, credential-free projections of Meta's non-text inbox payloads.

No URL is fetched here. Media previews are restricted to Meta's CDN; other
public HTTPS URLs remain explicit links. Keep raw provider metadata private.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

MAX_ATTACHMENTS = 30
_TYPES = {"image", "video", "audio", "file", "share", "unknown"}
_PREVIEW_HOSTS = ("fbcdn.net", "cdninstagram.com", "fbsbx.com")
_SECRET_QUERY_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "appsecret_proof",
    "client_secret",
    "password",
    "token",
    "api_key",
    "authorization",
}


def safe_attachment_url(value: object, *, preview: bool = False) -> str:
    """Allow public HTTPS links, never credentials, local targets or active schemes."""
    if not isinstance(value, str) or not value or len(value) > 8192:
        return ""
    if re.search(r"[\x00-\x20\x7f\\]", value):
        return ""
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
            return ""
        if not host or "." not in host or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
            return ""
        # Links/previews never need bare IP targets; reject ambiguous numeric hosts too.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return ""
        if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,63}", host) or host.replace(".", "").isdigit():
            return ""
        if any(key.lower() in _SECRET_QUERY_KEYS for key, _ in [*parse_qsl(parts.query), *parse_qsl(parts.fragment)]):
            return ""
        if preview and not any(host == domain or host.endswith("." + domain) for domain in _PREVIEW_HOSTS):
            return ""
        return value
    except (ValueError, UnicodeError):
        return ""


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list:
    if isinstance(value, dict):
        value = value.get("data", [])
    return value[:MAX_ATTACHMENTS] if isinstance(value, list) else []


def _text(value: object, limit: int = 500) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _normalize(item: object, *, share: bool = False) -> dict | None:
    if not isinstance(item, dict):
        return None
    payload = _dict(item.get("payload"))
    template = _dict(item.get("generic_template"))
    cta = _dict(template.get("cta"))
    image = _dict(item.get("image_data"))
    video = _dict(item.get("video_data"))
    raw_type = item.get("type") or item.get("mime_type") or ""
    raw_type = raw_type if isinstance(raw_type, str) else ""
    kind = (
        "share"
        if share or raw_type in ("share", "reel", "story", "ig_post", "ig_reel", "ig_story", "story_mention", "post")
        else raw_type
    )
    if raw_type == "fallback" or template:
        kind = "share"
    elif raw_type == "sticker":
        kind = "image"
    if kind not in _TYPES:
        if image or str(raw_type).startswith("image/"):
            kind = "image"
        elif video or isinstance(item.get("video_data"), str) or str(raw_type).startswith("video/"):
            kind = "video"
        elif str(raw_type).startswith("audio/"):
            kind = "audio"
        elif item.get("file_url") or item.get("mime_type"):
            kind = "file"
        else:
            kind = "unknown"
    url = safe_attachment_url(
        item.get("link")
        or item.get("url")
        or payload.get("url")
        or video.get("url")
        or (item.get("video_data") if isinstance(item.get("video_data"), str) else "")
        or image.get("url")
        or image.get("animated_gif_url")
        or cta.get("url")
        or item.get("file_url")
    )
    preview = safe_attachment_url(
        item.get("preview_url")
        or item.get("thumbnail_url")
        or image.get("preview_url")
        or image.get("animated_gif_preview_url")
        or image.get("url")
        or video.get("preview_url")
        or (url if kind == "image" else ""),
        preview=True,
    )
    result = {
        "type": kind,
        "url": url,
        "title": _text(item.get("title") or item.get("name") or payload.get("title") or template.get("title")),
        "preview_url": preview,
        "availability": "available" if url else "unavailable",
    }
    attachment_id = _text(
        item.get("id") or payload.get("ig_post_media_id") or payload.get("id") or payload.get("reel_video_id"), 255
    )
    if attachment_id:
        result["id"] = attachment_id
    return result


def _identity(item: dict) -> tuple[str, str, str]:
    if item.get("id"):
        return (item["type"], "id", item["id"])
    if item.get("url"):
        parts = urlsplit(item["url"])
        # Preserve query resource IDs (Meta's CDN uses asset_id); only discard
        # known rotating signature fields, never all query parameters.
        stable_query = [
            (key, value)
            for key, value in parse_qsl(parts.query)
            if key.lower() not in {"oh", "oe", "signature", "expires"}
            and not key.lower().startswith(("_nc_", "x-amz-"))
        ]
        return (item["type"], "url", parts.netloc + parts.path + "?" + urlencode(sorted(stable_query)))
    return (item["type"], "unavailable", item.get("title", ""))


def _combine(*groups: list[dict]) -> list[dict]:
    result: list[dict] = []
    identities: list[tuple] = []
    url_identities: list[tuple | None] = []
    for group in groups:
        for item in group:
            identity = _identity(item)
            url_identity = _identity({**item, "id": ""}) if item.get("url") else None
            match = None
            for index, previous in enumerate(result):
                if previous["type"] != item["type"]:
                    continue
                same_id = item.get("id") and item.get("id") == previous.get("id")
                same_url = url_identity is not None and url_identity == url_identities[index]
                if same_id or same_url or identities[index] == identity:
                    match = index
                    break
            if match is None:
                if len(result) < MAX_ATTACHMENTS:
                    result.append(dict(item))
                    identities.append(identity)
                    url_identities.append(url_identity)
                continue
            previous = result[match]
            combined = {**previous, **{k: v for k, v in item.items() if v or k not in previous}}
            combined["availability"] = "available" if combined.get("url") else "unavailable"
            result[match] = combined
            identities[match] = _identity(combined)
            url_identities[match] = _identity({**combined, "id": ""}) if combined.get("url") else None
    return result


def is_deleted_content(extra: object) -> bool:
    data = _dict(extra)
    return _dict(data.get("message")).get("is_deleted") is True or data.get("is_deleted") is True


def normalize_attachments(extra: object) -> list[dict]:
    """Read webhook, Graph and persisted normalized shapes; whitelist output keys."""
    data = _dict(extra)
    if is_deleted_content(data):
        return [{"type": "unknown", "url": "", "title": "", "preview_url": "", "availability": "unavailable"}]
    groups = []
    for source in (data, _dict(data.get("message"))):
        for key in ("attachments", "shares"):
            normalized = [_normalize(item, share=key == "shares") for item in _items(source.get(key))]
            groups.append([item for item in normalized if item is not None])
        story = _dict(source.get("story"))
        if story:
            item = _normalize(story, share=True)
            if item is not None:
                groups.append([item])
    # The persisted projection can have a fresher signed URL than the retained
    # original webhook. It is still revalidated, never trusted as HTML/URL.
    saved = [_normalize(item) for item in _items(data.get("inbox_attachments"))]
    groups.append([item for item in saved if item is not None])
    return _combine(*groups)


def _content_fields(extra: dict) -> dict:
    """Fingerprint source content only, excluding delivery/reply metadata."""
    fields = {key: extra[key] for key in ("attachments", "shares", "story") if key in extra}
    message = _dict(extra.get("message"))
    fields.update({f"message.{key}": message[key] for key in ("attachments", "shares", "story") if key in message})
    if not fields and "inbox_attachments" in extra:
        fields["inbox_attachments"] = extra["inbox_attachments"]
    return fields


def merge_message_extra(existing: object, incoming: object) -> dict:
    """Enrich duplicate DMs without replacing a rich webhook with a text-only poll."""
    old, new = _dict(existing), _dict(incoming)
    merged = {**old, **new}
    if isinstance(old.get("message"), dict) and isinstance(new.get("message"), dict):
        merged["message"] = {**old["message"], **new["message"]}
    if is_deleted_content(old) or is_deleted_content(new):
        # A text-only poll must not resurrect explicitly withdrawn content.
        merged["message"] = {**_dict(merged.get("message")), "is_deleted": True}
    previous_fields, incoming_fields = _content_fields(old), _content_fields(new)
    replay = bool(incoming_fields) and all(previous_fields.get(key) == value for key, value in incoming_fields.items())
    merged["inbox_attachments"] = (
        normalize_attachments(merged)
        if is_deleted_content(merged)
        else _combine(normalize_attachments(old), [] if replay else normalize_attachments(new))
    )
    return merged


def _provider_id(value: object) -> str:
    """Keep opaque provider IDs intact; never coerce objects into identities."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        return ""
    value = str(value)
    return value if value and len(value) <= 255 and not any(char.isspace() for char in value) else ""


def _identity_ids(value: object) -> list[str] | None:
    """Read a complete, small identity list, preserving ambiguity as unknown."""
    if isinstance(value, dict):
        if _dict(value.get("paging")).get("next"):
            return None
        value = value.get("data")
    if not isinstance(value, list) or len(value) > 100:
        return None
    ids = [_provider_id(item.get("id") if isinstance(item, dict) else item) for item in value]
    if any(not item for item in ids) or len(set(ids)) != len(ids):
        return None
    return ids


def _polled_message_identity(message: dict, *, own_id: str, sender_id: str, participant_ids: object) -> dict:
    """Project only proven one-to-one addressing, never handles or time proximity."""
    own_id, sender_id = _provider_id(own_id), _provider_id(sender_id)
    extra: dict = {}
    if own_id and sender_id == own_id:
        extra["direction"] = "outbound"

    participants = _identity_ids(participant_ids) if participant_ids is not None else []
    if participants is None:
        return extra
    # A known group cannot be converted to one-to-one by a message's `to` edge.
    if len(participants) > 2:
        extra["participant_ids"] = participants
        return extra
    if not sender_id:
        return extra

    recipients = _identity_ids(message["to"]) if "to" in message else None
    if "to" in message and (recipients is None or len(recipients) != 1):
        return extra
    recipient_id = recipients[0] if recipients else ""
    if recipient_id:
        if recipient_id == sender_id or (own_id and own_id not in (sender_id, recipient_id)):
            return extra
        if participants and set(participants) != {sender_id, recipient_id}:
            return extra
    elif len(participants) == 2 and own_id in participants and sender_id in participants:
        recipient_id = next(item for item in participants if item != sender_id)

    if recipient_id:
        if participants:
            extra["participant_ids"] = participants
        # `recipient_id` is the legacy reply target, not the message addressee.
        extra["message_recipient_id"] = recipient_id
    return extra


def polled_message_extra(
    message: dict,
    *,
    conversation_id: str,
    sender_id: str,
    own_id: str | None = None,
    participant_ids: object = None,
) -> dict:
    """Preserve content/reply addressing and optional verified history identities."""
    extra = {"conversation_id": conversation_id, "sender_id": sender_id}
    if own_id is not None:
        extra.update(
            _polled_message_identity(message, own_id=own_id, sender_id=sender_id, participant_ids=participant_ids)
        )
    for field in ("attachments", "shares", "story", "is_deleted", "is_unsupported"):
        if field in message:
            extra[field] = message[field]
    return merge_message_extra({}, extra)


# Meta's Message reference (also linked by Instagram Login Conversations API)
# documents attachments and requires explicit share subfields. The legacy
# fields remain the compatibility fallback for unsupported Graph node versions.
# https://developers.facebook.com/docs/graph-api/reference/message
BASIC_MESSAGE_FIELDS = "id,message,from,created_time"
CONTENT_MESSAGE_FIELDS = (
    BASIC_MESSAGE_FIELDS + ",attachments{id,name,image_data,video_data,file_url},shares{id,name,type,url}"
)


def request_with_content_fields(request, url: str, *, access_token: str, params: dict, basic_fields: str):
    """Retry only a specific unsupported-field rejection, never an auth/quota error."""
    from .exceptions import APIError

    try:
        return request("GET", url, access_token=access_token, params=params)
    except APIError as exc:
        error = _dict(_dict(exc.raw_response).get("error"))
        message = str(error.get("message") or "").lower()
        unsupported = (
            error.get("code") == 100
            and any(
                field in message
                for field in (
                    "attachments",
                    "shares",
                    "image_data",
                    "video_data",
                    "file_url",
                    "(name)",
                    "(type)",
                    "(url)",
                )
            )
            and any(
                term in message
                for term in ("nonexisting", "non-existing", "unknown field", "unsupported", "not supported")
            )
        )
        if not unsupported:
            raise
        return request("GET", url, access_token=access_token, params={**params, "fields": basic_fields})
