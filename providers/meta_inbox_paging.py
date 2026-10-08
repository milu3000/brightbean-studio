"""Bounded Meta inbox paging recovery; never treat an incomplete walk as empty."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlsplit

from .exceptions import APIError
from .meta_inbox_content import request_with_content_fields

PAGE_LIMITS = (50, 25, 10, 5, 1)
MAX_POLL_REQUESTS = 40


def is_page_size_rejection(status_code: object, error: object) -> bool:
    """Recognize only Graph's specific too-much-data refusal, not generic 500s."""
    if type(status_code) is not int or status_code not in (400, 500) or not isinstance(error, dict):
        return False
    message = error.get("message")
    return (
        type(error.get("code")) is int
        and error["code"] == 1
        and isinstance(message, str)
        and len(message) <= 4096
        and "please reduce the amount of data" in message.lower()
    )


def _paging_error(reason: str, platform: str) -> APIError:
    # Never include provider URLs, cursors, participants or response text here.
    return APIError(f"Inbox paging incomplete: {reason}", platform=platform)


class InboxRequestBudget:
    """One budget includes the initial nested request and every recovery GET."""

    def __init__(self, request, *, platform: str):
        self.request = request
        self.platform = platform
        self.used = 0

    def __call__(self, *args, **kwargs):
        if self.used >= MAX_POLL_REQUESTS:
            raise _paging_error("request budget exhausted", self.platform)
        self.used += 1
        return self.request(*args, **kwargs)


def _valid_cursor(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 1024 and re.fullmatch(r"[A-Za-z0-9_+/=-]+", value) is not None


def _edge_id(value: object, *, platform: str) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 255
        or value in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9_.:=+-]+", value) is None
    ):
        raise _paging_error("invalid conversation identity", platform)
    return quote(value, safe="")


def _next_cursor(data: dict, *, url: str, params: dict, aliases: tuple[str, ...], platform: str) -> str:
    paging = data.get("paging", {})
    if not isinstance(paging, dict):
        raise _paging_error("invalid pagination", platform)
    next_url = paging.get("next")
    if next_url is None:
        if any(source.get(key) for source in (data, paging) for key in ("has_more", "truncated", "is_truncated")):
            raise _paging_error("missing continuation cursor", platform)
        return ""
    if not isinstance(next_url, str) or not 0 < len(next_url) <= 8192 or re.search(r"[\x00-\x20\x7f\\]", next_url):
        raise _paging_error("invalid pagination route", platform)
    try:
        candidate = urlsplit(next_url)
        expected = urlsplit(url)
        paths = {urlsplit(route).path for route in (url, *aliases)}
        # Instagram may advertise v26 while the configured request uses v25.
        # Only the known version alias is accepted; we still request v25.
        if expected.netloc == "graph.instagram.com":
            paths |= {"/v26.0/" + path.removeprefix("/v25.0/") for path in paths if path.startswith("/v25.0/")}
        if (
            candidate.scheme != "https"
            or candidate.netloc != expected.netloc
            or candidate.path not in paths
            or candidate.username is not None
            or candidate.password is not None
            or candidate.fragment
        ):
            raise ValueError
        query = parse_qsl(candidate.query, keep_blank_values=True, strict_parsing=True, max_num_fields=50)
        after_values = [value for key, value in query if key == "after"]
        if len(after_values) != 1 or not _valid_cursor(after_values[0]):
            raise ValueError
        cursor = after_values[0]
        cursors = paging.get("cursors", {})
        if not isinstance(cursors, dict) or ("after" in cursors and cursors["after"] != cursor):
            raise ValueError
        if any(key in {"before", "offset", "until"} for key, _value in query):
            raise ValueError
        since_values = [value for key, value in query if key == "since"]
        if since_values and ("since" not in params or since_values != [str(params["since"])]):
            raise ValueError
        return cursor
    except (ValueError, UnicodeError) as exc:
        raise _paging_error("invalid pagination cursor or route", platform) from exc


def _pages(request, url: str, *, access_token: str, params: dict, platform: str, basic_fields=None, aliases=()):
    """Resize only the refused page, keeping its pinned route, filters and cursor."""
    params = dict(params)
    limit_index = 0
    basic_fallback: list[str] = []
    seen_cursors: set[str] = set()
    if "after" in params:
        if not _valid_cursor(params["after"]):
            raise _paging_error("invalid starting cursor", platform)
        seen_cursors.add(params["after"])
    while True:
        try:
            page_params = {**params, "limit": PAGE_LIMITS[limit_index]}
            if basic_fallback:
                page_params["fields"] = basic_fields
            if basic_fields is None or basic_fallback:
                response = request("GET", url, access_token=access_token, params=page_params)
            else:
                response = request_with_content_fields(
                    request,
                    url,
                    access_token=access_token,
                    params=page_params,
                    basic_fields=basic_fields,
                    on_fallback=basic_fallback.append,
                )
        except APIError as exc:
            error = exc.raw_response.get("error") if isinstance(exc.raw_response, dict) else None
            if not is_page_size_rejection(exc.status_code, error) or limit_index == len(PAGE_LIMITS) - 1:
                raise
            limit_index += 1
            continue
        data = response.json()
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("data"), list)
            or len(data["data"]) > PAGE_LIMITS[0]
            or any(not isinstance(item, dict) for item in data["data"])
        ):
            raise _paging_error("invalid page payload", platform)
        after = _next_cursor(data, url=url, params=params, aliases=aliases, platform=platform)
        if after and after in seen_cursors:
            raise _paging_error("repeated cursor", platform)
        yield data["data"], "basic_fallback" if basic_fallback else "fields_requested"
        if not after:
            return
        seen_cursors.add(after)
        params["after"] = after


def segmented_conversations(
    request,
    *,
    api_base: str,
    access_token: str,
    params: dict,
    message_fields: str,
    basic_fields: str,
    platform: str,
    own_id: str = "",
) -> list[dict]:
    """Recover a refused nested request as independently bounded edge walks.

    Return only after every edge finishes. No truncated prefix can become a
    successful empty poll or advance the caller's stream watermark.
    """
    conversations = []
    aliases = (f"{api_base}/{_edge_id(own_id, platform=platform)}/conversations",) if own_id else ()
    filters = {key: value for key, value in params.items() if key != "fields"}
    for page, _status in _pages(
        request,
        f"{api_base}/me/conversations",
        access_token=access_token,
        params={**filters, "fields": "id,participants{id}"},
        platform=platform,
        aliases=aliases,
    ):
        for conversation in page:
            native_id = _edge_id(conversation.get("id"), platform=platform)
            # A conversations cursor is not a messages-edge cursor. `since`
            # remains pinned, while each new message edge starts at its head.
            message_params = {key: value for key, value in filters.items() if key != "after"}
            for messages, status in _pages(
                request,
                f"{api_base}/{native_id}/messages",
                access_token=access_token,
                params={**message_params, "fields": message_fields},
                basic_fields=basic_fields,
                platform=platform,
            ):
                conversations.append({**conversation, "messages": {"data": messages}, "_content_fetch_status": status})
    return conversations
