"""Plain server-rendered inbox recovery using the same authorized saved reads."""

from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import canonical_reads as reader
from . import preserved_history

PAGE_SIZE = 20


def _url(name, workspace_id, *, conversation_id=None, message_id=None, query=None):
    kwargs = {"workspace_id": workspace_id}
    if conversation_id is not None:
        kwargs["conversation_id"] = conversation_id
    if message_id is not None:
        kwargs["message_id"] = message_id
    value = reverse("inbox:" + name, kwargs=kwargs)
    parameters = {key: value for key, value in (query or {}).items() if value not in (None, "")}
    return value + ("?" + urlencode(parameters) if parameters else "")


def _render(request, template, workspace_id, values=None, *, status=200):
    context = {
        "canonical_url": _url("basic_feed", workspace_id),
        "preserved_url": _url("basic_preserved_feed", workspace_id),
        "unassigned_url": _url("basic_unassigned_feed", workspace_id),
        **(values or {}),
    }
    response = render(request, "inbox/basic/" + template, context, status=status)
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _error(request, workspace_id, exc):
    status = 422 if exc.code.startswith("invalid_") else 404 if exc.code == "not_found_or_denied" else 409
    detail = (
        "This page changed. Open the newest page."
        if exc.code.startswith("stale_")
        else "This history is unavailable with the current account access."
    )
    return _render(request, "error.html", workspace_id, {"detail": detail}, status=status)


@login_required
@require_GET
@never_cache
def basic_feed(request, workspace_id):
    scope = reader.session_read_scope(request.user, workspace_id)
    filters = {
        "search": request.GET.get("q", ""),
        "platform": request.GET.get("platform") or None,
        "social_account_id": request.GET.get("account") or None,
    }
    try:
        page = reader.list_conversations(scope, **filters, cursor=request.GET.get("cursor"), limit=PAGE_SIZE)
        accounts = reader.available_accounts(scope)
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    query = {"q": filters["search"], "platform": filters["platform"], "account": filters["social_account_id"]}
    for item in page["conversations"]:
        item["detail_url"] = _url("basic_detail", workspace_id, conversation_id=item["id"])
        if item["latest_message"]:
            _content_links(workspace_id, item["id"], item["latest_message"])
    return _render(
        request,
        "feed.html",
        workspace_id,
        {
            "page": page,
            "accounts": accounts,
            "filters": query,
            "newest_url": _url("basic_feed", workspace_id, query=query),
            "older_url": _url("basic_feed", workspace_id, query={**query, "cursor": page["next_cursor"]})
            if page["next_cursor"]
            else None,
        },
    )


@login_required
@require_GET
@never_cache
def basic_detail(request, workspace_id, conversation_id):
    scope = reader.session_read_scope(request.user, workspace_id)
    try:
        page = reader.read_conversation(scope, conversation_id, cursor=request.GET.get("cursor"), limit=PAGE_SIZE)
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    page.pop("read_ack_token", None)
    page.pop("composer_observation_token", None)
    for message in page["messages"] + page["undated_messages"]:
        _content_links(workspace_id, conversation_id, message)
    newest = _url("basic_detail", workspace_id, conversation_id=conversation_id)
    return _render(
        request,
        "detail.html",
        workspace_id,
        {
            "page": page,
            "newest_url": newest,
            "older_url": newest + "?" + urlencode({"cursor": page["next_cursor"]}) if page["next_cursor"] else None,
            "undated_url": newest + "?" + urlencode({"cursor": page["undated_next_cursor"]})
            if page["undated_next_cursor"]
            else None,
        },
    )


@login_required
@require_GET
@never_cache
def basic_preserved_feed(request, workspace_id):
    scope = reader.session_read_scope(request.user, workspace_id)
    filters = {"q": request.GET.get("q", ""), "account": request.GET.get("account", "")}
    try:
        page = preserved_history.list_records(
            scope,
            social_account_id=filters["account"] or None,
            search=filters["q"],
            cursor=request.GET.get("cursor"),
            limit=PAGE_SIZE,
        )
        accounts = preserved_history.available_accounts(scope)
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    for item in page["records"]:
        item["detail_url"] = _url("basic_preserved_detail", workspace_id, message_id=item["id"])
        item["body_url"] = item["detail_url"] if item["body_truncated"] else None
        item["attachments_url"] = item["detail_url"] if item["attachments_truncated"] else None
    return _render(
        request,
        "preserved_feed.html",
        workspace_id,
        {
            "page": page,
            "accounts": accounts,
            "filters": filters,
            "newest_url": _url("basic_preserved_feed", workspace_id, query=filters),
            "older_url": _url("basic_preserved_feed", workspace_id, query={**filters, "cursor": page["next_cursor"]})
            if page["next_cursor"]
            else None,
        },
    )


@login_required
@require_GET
@never_cache
def basic_preserved_detail(request, workspace_id, message_id):
    scope = reader.session_read_scope(request.user, workspace_id)
    try:
        record = preserved_history.read_record(scope, message_id)
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    from .preserved_details import list_related

    try:
        replies = list_related(scope, message_id, kind="replies")
        notes = list_related(scope, message_id, kind="notes")
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    for page in (replies, notes):
        _related_links(workspace_id, message_id, page)
    record["body_url"] = (
        _url("basic_preserved_content", workspace_id, message_id=message_id, query={"part": "body"})
        if record["body_truncated"]
        else None
    )
    record["attachments_url"] = (
        _url("basic_preserved_content", workspace_id, message_id=message_id, query={"part": "attachments"})
        if record["attachments_truncated"]
        else None
    )
    return _render(
        request, "preserved_detail.html", workspace_id, {"record": record, "replies": replies, "notes": notes}
    )


# Membership middleware normally saves navigation preference. Recovery GETs
# preserve that preference too; no acknowledgement or other state is written.
for _view in (basic_feed, basic_detail, basic_preserved_feed, basic_preserved_detail):
    setattr(_view, "preserve_workspace_preference", True)  # noqa: B010 - callable middleware metadata


def _content_links(workspace_id, conversation_id, message):
    message["body_url"] = (
        _url(
            "basic_message_content",
            workspace_id,
            conversation_id=conversation_id,
            message_id=message["id"],
            query={"part": "body"},
        )
        if message["body_truncated"]
        else None
    )
    message["attachments_url"] = (
        _url(
            "basic_message_content",
            workspace_id,
            conversation_id=conversation_id,
            message_id=message["id"],
            query={"part": "attachments"},
        )
        if message["attachments_truncated"]
        else None
    )


@login_required
@require_GET
@never_cache
def basic_message_content(request, workspace_id, conversation_id, message_id):
    scope = reader.session_read_scope(request.user, workspace_id)
    part = request.GET.get("part", "body")
    try:
        if part == "body":
            value = reader.read_message_body(scope, message_id, cursor=request.GET.get("cursor"))
            value["attachments"] = []
        elif part == "attachments":
            value = reader.read_message_attachments(scope, message_id, cursor=request.GET.get("cursor"))
            # Bind the message path to the actual authorized conversation too.
            body = reader.read_message_body(scope, message_id, limit=1)
            value.update(
                conversation_id=body["conversation_id"],
                body="",
                available=body["available"],
                attachments=value["attachments"] if "attachments" in value else value["items"],
            )
        else:
            raise reader.CanonicalReadError("invalid_filter", "Unknown message section.")
        if value["conversation_id"] != str(conversation_id):
            raise reader.CanonicalReadError("not_found_or_denied", "The message is unavailable in this conversation.")
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    newest = _url(
        "basic_message_content",
        workspace_id,
        conversation_id=conversation_id,
        message_id=message_id,
        query={"part": part},
    )
    return _render(
        request,
        "content.html",
        workspace_id,
        {
            "content": value,
            "newest_url": newest,
            "next_url": newest + "&" + urlencode({"cursor": value["next_cursor"]}) if value["next_cursor"] else None,
            "back_url": _url("basic_detail", workspace_id, conversation_id=conversation_id),
        },
    )


def _related_links(workspace_id, message_id, page):
    for item in page["records"]:
        item["body_url"] = (
            _url(
                "basic_preserved_content",
                workspace_id,
                message_id=message_id,
                query={"part": page["kind"], "item": item["id"]},
            )
            if item["body_truncated"]
            else None
        )
    page["older_url"] = (
        _url(
            "basic_preserved_related",
            workspace_id,
            message_id=message_id,
            query={"kind": page["kind"], "cursor": page["next_cursor"]},
        )
        if page["next_cursor"]
        else None
    )


@login_required
@require_GET
@never_cache
def basic_preserved_related(request, workspace_id, message_id):
    from .preserved_details import list_related

    scope = reader.session_read_scope(request.user, workspace_id)
    try:
        page = list_related(
            scope, message_id, kind=request.GET.get("kind", "replies"), cursor=request.GET.get("cursor")
        )
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    _related_links(workspace_id, message_id, page)
    return _render(
        request,
        "related.html",
        workspace_id,
        {"page": page, "back_url": _url("basic_preserved_detail", workspace_id, message_id=message_id)},
    )


@login_required
@require_GET
@never_cache
def basic_preserved_content(request, workspace_id, message_id):
    from .preserved_details import read_part

    scope = reader.session_read_scope(request.user, workspace_id)
    part = request.GET.get("part", "body")
    query = {"part": part, "item": request.GET.get("item")}
    try:
        value = read_part(
            scope,
            message_id,
            part=part,
            item_id=query["item"],
            cursor=request.GET.get("cursor"),
            limit=3 if part == "attachments" else 2000,
        )
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    newest = _url("basic_preserved_content", workspace_id, message_id=message_id, query=query)
    return _render(
        request,
        "content.html",
        workspace_id,
        {
            "content": value,
            "newest_url": newest,
            "next_url": newest + "&" + urlencode({"cursor": value["next_cursor"]}) if value["next_cursor"] else None,
            "back_url": _url("basic_preserved_detail", workspace_id, message_id=message_id),
        },
    )


for _content_view in (basic_message_content, basic_preserved_related, basic_preserved_content):
    setattr(_content_view, "preserve_workspace_preference", True)  # noqa: B010 - callable middleware metadata


@login_required
@require_GET
@never_cache
def basic_unassigned_feed(request, workspace_id):
    scope = reader.session_read_scope(request.user, workspace_id)
    filters = {"q": request.GET.get("q", ""), "account": request.GET.get("account", "")}
    try:
        page = reader.list_unassigned_messages(
            scope,
            social_account_id=filters["account"] or None,
            search=filters["q"],
            cursor=request.GET.get("cursor"),
            limit=PAGE_SIZE,
        )
        accounts = reader.available_accounts(scope)
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    for message in page["messages"]:
        message["detail_url"] = _url("basic_unassigned_detail", workspace_id, message_id=message["id"])
        message["body_url"] = message["detail_url"] if message["body_truncated"] else None
        message["attachments_url"] = message["detail_url"] if message["attachments_truncated"] else None
    return _render(
        request,
        "unassigned_feed.html",
        workspace_id,
        {
            "page": page,
            "accounts": accounts,
            "filters": filters,
            "newest_url": _url("basic_unassigned_feed", workspace_id, query=filters),
            "older_url": _url("basic_unassigned_feed", workspace_id, query={**filters, "cursor": page["next_cursor"]})
            if page["next_cursor"]
            else None,
        },
    )


@login_required
@require_GET
@never_cache
def basic_unassigned_detail(request, workspace_id, message_id):
    scope = reader.session_read_scope(request.user, workspace_id)
    try:
        message = reader.read_unassigned_message(scope, message_id)["message"]
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    message["body_url"] = (
        _url("basic_unassigned_content", workspace_id, message_id=message_id, query={"part": "body"})
        if message["body_truncated"]
        else None
    )
    message["attachments_url"] = (
        _url("basic_unassigned_content", workspace_id, message_id=message_id, query={"part": "attachments"})
        if message["attachments_truncated"]
        else None
    )
    return _render(request, "unassigned_detail.html", workspace_id, {"message": message})


@login_required
@require_GET
@never_cache
def basic_unassigned_content(request, workspace_id, message_id):
    scope = reader.session_read_scope(request.user, workspace_id)
    part = request.GET.get("part", "body")
    try:
        if part == "body":
            value = reader.read_unassigned_message_body(scope, message_id, cursor=request.GET.get("cursor"))
            value["attachments"] = []
        elif part == "attachments":
            value = reader.read_unassigned_message_attachments(scope, message_id, cursor=request.GET.get("cursor"))
            proof = reader.read_unassigned_message_body(scope, message_id, limit=1)
            value.update(body="", attachments=value["items"], available=proof["available"])
        else:
            raise reader.CanonicalReadError("invalid_filter", "Unknown saved content section.")
    except reader.CanonicalReadError as exc:
        return _error(request, workspace_id, exc)
    start = _url("basic_unassigned_content", workspace_id, message_id=message_id, query={"part": part})
    return _render(
        request,
        "content.html",
        workspace_id,
        {
            "content": value,
            "newest_url": start,
            "next_url": start + "&" + urlencode({"cursor": value["next_cursor"]}) if value["next_cursor"] else None,
            "back_url": _url("basic_unassigned_detail", workspace_id, message_id=message_id),
        },
    )


for _unassigned_view in (basic_unassigned_feed, basic_unassigned_detail, basic_unassigned_content):
    setattr(_unassigned_view, "preserve_workspace_preference", True)  # noqa: B010 - callable middleware metadata
