"""Bounded, credential-free projections of Meta's non-text inbox payloads.

No URL is fetched here. Media previews are restricted to Meta's CDN; other
public HTTPS URLs remain explicit links. Keep raw provider metadata private.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

MAX_ATTACHMENTS = 30
ContentStatus = Literal[
    "removed", "partial", "unsupported", "fields_unavailable", "link_provided", "unavailable", "text", "no_metadata"
]
_TYPES = {"image", "video", "audio", "file", "share", "unknown"}
_AVAILABILITY_REASONS = {
    "link_provided",
    "missing_url",
    "unsafe_url",
    "preview_only",
    "unsupported",
    "removed",
    "fields_unavailable",
    "invalid_metadata",
}
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


def shared_content_url(value: object) -> str:
    """Recognize an actually supplied post URL; never construct one from an ID."""
    url = safe_attachment_url(value)
    if not url:
        return ""
    parts = urlsplit(url)
    host, path = (parts.hostname or "").lower(), parts.path
    if host in {"instagram.com", "www.instagram.com"}:
        return url if re.match(r"^/(?:p|reel|reels|tv|stories)/[^/]+", path) else ""
    if host in {"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com"}:
        return (
            url
            if (
                re.search(r"/(?:posts|photos|videos|reel|share)/[^/]+", path)
                or path in {"/permalink.php", "/story.php", "/photo.php", "/watch/"}
                and bool(parts.query)
            )
            else ""
        )
    if host in {"threads.net", "www.threads.net", "threads.com", "www.threads.com"}:
        return url if re.match(r"^/@[^/]+/post/[^/]+", path) else ""
    if host == "fb.watch":
        return url if path.strip("/") else ""
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
    candidates: tuple[Any, ...] = (
        item.get("link"),
        item.get("url"),
        payload.get("url"),
        video.get("url"),
        item.get("video_data") if isinstance(item.get("video_data"), str) else "",
        image.get("url"),
        image.get("animated_gif_url"),
        cta.get("url"),
        item.get("file_url"),
    )
    if kind == "share":
        candidates = (
            item.get("permalink"),
            item.get("permalink_url"),
            payload.get("permalink"),
            payload.get("permalink_url"),
            item.get("link"),
            cta.get("url"),
            *candidates,
        )
    urls = [safe for value in candidates if (safe := safe_attachment_url(value))]
    url = next((post for value in urls if (post := shared_content_url(value))), "") if kind == "share" else ""
    url = url or next(iter(urls), "")
    preview_candidates = (
        item.get("preview_url"),
        item.get("thumbnail_url"),
        payload.get("preview_url"),
        payload.get("thumbnail_url"),
        image.get("preview_url"),
        image.get("animated_gif_preview_url"),
        image.get("url"),
        template.get("image_url"),
        video.get("preview_url"),
        url if kind == "image" else "",
    )
    preview = next((safe for value in preview_candidates if (safe := safe_attachment_url(value, preview=True))), "")
    result = {
        "type": kind,
        "url": url,
        "title": _text(item.get("title") or item.get("name") or payload.get("title") or template.get("title")),
        "preview_url": preview,
        "availability": "available" if url else "unavailable",
        "availability_reason": (
            "link_provided"
            if url
            else "preview_only"
            if preview
            else item.get("availability_reason")
            if isinstance(item.get("availability_reason"), str)
            and item.get("availability_reason") in _AVAILABILITY_REASONS
            else "unsafe_url"
            if any(candidates)
            else "missing_url"
        ),
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
    unavailable_occurrences: list[int | None] = []
    for group in groups:
        # Unknown items have no deduplication identity. Preserve their count in
        # one provider array; merge only the corresponding occurrence across
        # repeated observations instead of collapsing six missing photos to one.
        occurrences: dict[tuple, int] = {}
        for item in group:
            identity = _identity(item)
            occurrence = None
            if not item.get("id") and not item.get("url"):
                occurrences[identity] = occurrences.get(identity, 0) + 1
                occurrence = occurrences[identity]
            url_identity = _identity({**item, "id": ""}) if item.get("url") else None
            match = None
            for index, previous in enumerate(result):
                if previous["type"] != item["type"]:
                    continue
                same_id = item.get("id") and item.get("id") == previous.get("id")
                same_url = url_identity is not None and url_identity == url_identities[index]
                same_fallback = identities[index] == identity and (
                    occurrence is None or unavailable_occurrences[index] == occurrence
                )
                if same_id or same_url or same_fallback:
                    match = index
                    break
            if match is None:
                if len(result) < MAX_ATTACHMENTS:
                    result.append(dict(item))
                    identities.append(identity)
                    url_identities.append(url_identity)
                    unavailable_occurrences.append(occurrence)
                continue
            previous = result[match]
            combined = {**previous, **{k: v for k, v in item.items() if v or k not in previous}}
            if (
                item["type"] == "share"
                and shared_content_url(previous.get("url"))
                and not shared_content_url(item.get("url"))
            ):
                combined["url"] = previous["url"]
            combined["availability"] = "available" if combined.get("url") else "unavailable"
            if combined.get("url"):
                combined["availability_reason"] = "link_provided"
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
        return []
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
    # Availability of the message is separate from actual attachment entries.
    # Missing/unsupported provider fields must never fabricate a counted item.
    return _combine(*groups)


def _has_nonempty_content(value):
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        return bool(value["data"])
    return bool(value)


def message_content_status(extra: object, body: str = "") -> ContentStatus:
    """Describe observed content metadata without claiming any URL was fetched."""
    data = _dict(extra)
    if is_deleted_content(data):
        return "removed"
    attachments = normalize_attachments(data)
    if any(source.get("is_unsupported") is True for source in (data, _dict(data.get("message")))):
        return "partial" if attachments or body else "unsupported"
    if data.get("content_fetch_status") == "basic_fallback":
        return "fields_unavailable"
    if any(item["url"] for item in attachments):
        return "link_provided"
    if attachments:
        return "unavailable"
    if any(
        _has_nonempty_content(source.get(key))
        for source in (data, _dict(data.get("message")))
        for key in ("attachments", "shares", "story")
    ):
        return "unavailable"
    return "text" if body else "no_metadata"


def merged_content_status(previous, extra, *, body, attachments, deleted=False):
    """Retain bounded incomplete-content evidence independently of media items."""
    data = _dict(extra)
    if deleted or is_deleted_content(data):
        return "removed"
    combined = {**data, "inbox_attachments": attachments}
    current = message_content_status(combined, body)
    explicitly_supported = any(source.get("is_unsupported") is False for source in (data, _dict(data.get("message"))))
    if previous in {"partial", "unsupported"} and not explicitly_supported:
        return "partial" if body or attachments else "unsupported"
    if current in {"partial", "unsupported", "fields_unavailable"}:
        return current
    if previous == "fields_unavailable" and data.get("content_fetch_status") != "fields_requested":
        return "fields_unavailable"
    return current


def merge_content_status_evidence(left, right, *, body, attachments, deleted=False):
    """Merging exact canonical/local rows cannot erase either row's warning."""
    prior = next(
        (status for status in ("partial", "unsupported", "fields_unavailable") if status in (left, right)), "unknown"
    )
    return merged_content_status(prior, {}, body=body, attachments=attachments, deleted=deleted)


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
    if merged.get("content_fetch_status") == "fields_requested":
        # A successful richer request supersedes a previous compatibility
        # fallback warning; it does not erase actual attachment metadata.
        merged["inbox_attachments"] = [
            item for item in merged["inbox_attachments"] if item.get("availability_reason") != "fields_unavailable"
        ]
    _merge_identity_metadata(old, new, merged)
    return merged


def _provider_id(value: object) -> str:
    """Keep opaque provider IDs intact; never coerce objects into identities."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        return ""
    value = str(value)
    return value if value and len(value) <= 255 and not any(char.isspace() for char in value) else ""


def _identity_ids(value: object) -> list[str] | None:
    """Read complete, bounded identity evidence; never drop malformed entries."""
    if isinstance(value, dict):
        paging = value.get("paging", {})
        if (
            not isinstance(paging, dict)
            or paging.get("next")
            or paging.get("previous")
            or value.get("has_more")
            or value.get("truncated")
            or value.get("is_truncated")
        ):
            return None
        total = _dict(value.get("summary")).get("total_count")
        data = value.get("data")
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or not isinstance(data, list) or total != len(data)
        ):
            return None
        value = data
    if not isinstance(value, list) or not value or len(value) > 100:
        return None
    ids = [_provider_id(item.get("id") if isinstance(item, dict) else item) for item in value]
    if any(not item for item in ids) or len(set(ids)) != len(ids):
        return None
    return ids


def merge_conversation_classification(old_type, old_reason, new_type, new_reason):
    """Missing observations cannot erase evidence; contradictions remain held."""
    if old_type == "group" or new_type == "group":
        return "group", "participants_group"
    if old_reason == "identity_conflict" or new_reason == "identity_conflict":
        return "unknown", "identity_conflict"
    if new_reason == "participants_missing" and old_reason != "participants_missing":
        return old_type, old_reason
    if old_type == "direct" and new_type == "unknown":
        return "unknown", "identity_conflict"
    return new_type, new_reason


def classify_conversation_identity(extra: object, *, own_ids, sender_id="") -> tuple[str, str, str]:
    """Endpoints prove direction, not a complete one-to-one participant set.

    Return type, bounded evidence reason, and a peer only for verified pairs.
    No raw participant metadata is copied into the conversation ledger.
    """
    data = _dict(extra)
    own = {_provider_id(value) for value in own_ids} - {""}
    sender = (
        _provider_id(sender_id)
        or _provider_id(data.get("sender_id"))
        or _provider_id(_dict(data.get("sender")).get("id"))
    )
    recipient = _provider_id(data.get("message_recipient_id")) or _provider_id(_dict(data.get("recipient")).get("id"))
    # These bounded markers preserve invalidity after projection/metadata merge.
    reason = data.get("classification_reason")
    if isinstance(reason, str) and reason in {
        "identity_conflict",
        "participants_invalid",
        "participants_incomplete",
        "participant_endpoints_conflict",
    }:
        return "unknown", reason, ""
    if data.get("conversation_type") == "group" and reason == "participants_group":
        return "group", "participants_group", ""
    sender_values = [
        value
        for value in (sender_id, data.get("sender_id"), _dict(data.get("sender")).get("id"))
        if value is not None and value != ""
    ]
    recipient_values = [
        value
        for value in (data.get("message_recipient_id"), _dict(data.get("recipient")).get("id"))
        if value is not None and value != ""
    ]
    for values in (sender_values, recipient_values):
        valid = [_provider_id(value) for value in values]
        if any(not value for value in valid) or len(set(valid)) > 1:
            return "unknown", "participant_endpoints_conflict", ""
    if "participant_ids" in data and "participants" in data:
        normalized, raw_ids = _identity_ids(data["participant_ids"]), _identity_ids(data["participants"])
        if normalized is None or raw_ids is None or set(normalized) != set(raw_ids):
            return "unknown", "identity_conflict", ""
    key = "participant_ids" if "participant_ids" in data else "participants"
    if key not in data:
        return "unknown", "participants_missing", ""
    raw = data[key]
    participants = _identity_ids(raw)
    if participants is None:
        incomplete = isinstance(raw, dict) and (
            _dict(raw.get("paging")).get("next")
            or _dict(raw.get("paging")).get("previous")
            or raw.get("has_more")
            or raw.get("truncated")
            or raw.get("is_truncated")
            or "total_count" in _dict(raw.get("summary"))
        )
        return "unknown", "participants_incomplete" if incomplete else "participants_invalid", ""
    identities = set(participants)
    if (
        not identities & own
        or not sender
        or sender not in identities
        or (recipient and recipient not in identities)
        or sender == recipient
    ):
        return "unknown", "participant_endpoints_conflict", ""
    if len(identities) > 2:
        return "group", "participants_group", ""
    peers = identities - own
    if len(identities) == 2 and len(peers) == 1:
        return "direct", "participants_pair", next(iter(peers))
    return "unknown", "participants_invalid", ""


def _merge_identity_metadata(old: dict, new: dict, merged: dict) -> None:
    """Do not let text-only polls or direct replays erase group/conflict proof."""
    old_type, old_reason = (
        old.get("conversation_type", "unknown"),
        old.get("classification_reason", "participants_missing"),
    )
    new_type, new_reason = (
        new.get("conversation_type", "unknown"),
        new.get("classification_reason", "participants_missing"),
    )
    if "conversation_type" in old or "conversation_type" in new:
        merged["conversation_type"], merged["classification_reason"] = merge_conversation_classification(
            old_type, old_reason, new_type, new_reason
        )
    old_ids = _identity_ids(old.get("participant_ids", old.get("participants")))
    new_ids = _identity_ids(new.get("participant_ids", new.get("participants")))
    if old_type == "group":
        if old_ids:
            merged["participant_ids"] = old_ids
        merged["conversation_type"] = "group"
        merged["classification_reason"] = "participants_group"
    elif old_ids and len(old_ids) > 2:
        # Keep prior complete evidence, but let the account-aware classifier
        # validate membership before making any new group claim.
        merged["participant_ids"] = old_ids
        if merged.get("classification_reason") != "identity_conflict":
            merged.pop("conversation_type", None)
            merged.pop("classification_reason", None)
    elif (
        merged.get("classification_reason") == "identity_conflict"
        or (
            old_ids
            and len(old_ids) == 2
            and any(key in new for key in ("participant_ids", "participants"))
            and new_ids is None
        )
        or (old_ids and new_ids and set(old_ids) != set(new_ids) and len(new_ids) <= 2)
    ):
        merged["conversation_type"] = "unknown"
        merged["classification_reason"] = "identity_conflict"
    elif new.get("classification_reason") == "participants_missing" and old_ids:
        for key in ("conversation_type", "classification_reason"):
            if key in old:
                merged[key] = old[key]
            else:
                merged.pop(key, None)


def _polled_message_identity(message: dict, *, own_id: str, sender_id: str, participant_ids: object) -> dict:
    """Project bounded classification evidence without equating `to` with a DM."""
    own_id, sender_id = _provider_id(own_id), _provider_id(sender_id)
    extra: dict = {}
    if own_id and sender_id == own_id:
        extra["direction"] = "outbound"
    recipients = _identity_ids(message["to"]) if "to" in message else None
    recipient = recipients[0] if recipients and len(recipients) == 1 else ""
    evidence = {"participant_ids": participant_ids} if participant_ids is not None else {}
    if recipient:
        evidence["message_recipient_id"] = recipient
    kind, reason, _peer = classify_conversation_identity(evidence, own_ids=[own_id], sender_id=sender_id)
    participants = _identity_ids(participant_ids)
    if "to" in message:
        if kind == "group":
            if recipients is None or sender_id in recipients or not set(recipients).issubset(participants or []):
                kind, reason = "unknown", "participant_endpoints_conflict"
        elif not recipient or sender_id == recipient or own_id not in {sender_id, recipient}:
            kind, reason = "unknown", "participant_endpoints_conflict"
    extra.update(conversation_type=kind, classification_reason=reason)
    if kind in {"direct", "group"}:
        extra["participant_ids"] = participants
    if kind == "direct" and participants:
        extra["message_recipient_id"] = recipient or next(item for item in participants if item != sender_id)
    elif kind == "unknown" and reason == "participants_missing" and recipient:
        # Addressing may still establish direction, never thread membership.
        extra["message_recipient_id"] = recipient
    return extra


def polled_conversation_classification(message: dict, *, own_id: str, sender_id: str, participant_ids: object) -> dict:
    """Classify already-returned participants without retaining their identities."""
    projection = _polled_message_identity(message, own_id=own_id, sender_id=sender_id, participant_ids=participant_ids)
    return {key: projection[key] for key in ("conversation_type", "classification_reason")}


def polled_message_extra(
    message: dict,
    *,
    conversation_id: str,
    sender_id: str,
    own_id: str | None = None,
    participant_ids: object = None,
    classification_summary: dict | None = None,
    content_fetch_status: str | None = None,
) -> dict:
    """Preserve content/reply addressing and optional verified history identities."""
    extra = {"conversation_id": conversation_id, "sender_id": sender_id}
    if classification_summary is not None:
        extra.update(
            {
                key: classification_summary[key]
                for key in ("conversation_type", "classification_reason")
                if key in classification_summary
            }
        )
    if content_fetch_status in {"basic_fallback", "fields_requested"}:
        extra["content_fetch_status"] = content_fetch_status
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


def request_with_content_fields(
    request, url: str, *, access_token: str, params: dict, basic_fields: str, on_fallback=None, optional_fields=None
):
    """Retry only a specific unsupported-field rejection, never an auth/quota error."""
    from .exceptions import APIError

    try:
        return request("GET", url, access_token=access_token, params=params)
    except APIError as exc:
        error = _dict(_dict(exc.raw_response).get("error"))
        message = str(error.get("message") or "").lower()
        fields = (
            optional_fields
            if optional_fields is not None
            else ("attachments", "shares", "image_data", "video_data", "file_url", "(name)", "(type)", "(url)")
        )
        unsupported = (
            exc.status_code == 400
            and error.get("code") == 100
            and any(field in message for field in fields)
            and any(
                term in message
                for term in ("nonexisting", "non-existing", "unknown field", "unsupported", "not supported")
            )
        )
        if not unsupported:
            raise
        if on_fallback is not None:
            on_fallback("basic_fallback")
        return request("GET", url, access_token=access_token, params={**params, "fields": basic_fields})
