"""Read-only pages for saved messages whose conversation has not been identified."""

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount

from . import canonical_reads as reader
from .canonical_views import _error
from .message_details import retained_capabilities


@login_required
@require_permission("use_inbox")
@require_GET
@never_cache
def feed(request, workspace_id):
    from .views import _get_workspace

    workspace = _get_workspace(request, workspace_id)
    if not reader.enabled():
        return HttpResponse("Not found.", status=404)
    if any(value.strip() for value in request.GET.getlist("sentiment")):
        return HttpResponse("Sentiment filtering has been retired.", status=400)
    if any(len(request.GET.getlist(key)) > 1 for key in ("q", "account", "platform", "cursor")):
        return HttpResponse("Choose one value per inbox filter.", status=422)
    try:
        scope = reader.session_read_scope(request.user, workspace.pk)
        page = reader.list_unassigned_messages(
            scope,
            social_account_id=request.GET.get("account") or None,
            platform=request.GET.get("platform") or None,
            search=request.GET.get("q", ""),
            cursor=request.GET.get("cursor") or None,
        )
        accounts = reader.available_accounts(scope)
    except reader.CanonicalReadError as exc:
        return _error(exc)
    params = request.GET.copy()
    params["cursor"] = page["next_cursor"] or ""
    return render(
        request,
        "inbox/unassigned_feed.html",
        {
            "workspace": workspace,
            "page": page,
            "accounts": accounts,
            "platform_choices": [
                (value, label)
                for value, label in SocialAccount._meta.get_field("platform").choices
                if value in {row["platform"] for row in accounts}
            ],
            "search_query": request.GET.get("q", ""),
            "active_account": request.GET.get("account", ""),
            "active_platform": request.GET.get("platform", ""),
            "next_url": reverse("inbox:unassigned_feed", kwargs={"workspace_id": workspace.pk})
            + "?"
            + params.urlencode()
            if page["next_cursor"]
            else "",
        },
    )


@login_required
@require_permission("use_inbox")
@require_GET
@never_cache
def detail(request, workspace_id, message_id):
    from .views import _get_workspace

    workspace = _get_workspace(request, workspace_id)
    if not reader.enabled():
        return HttpResponse("Not found.", status=404)
    try:
        scope = reader.session_read_scope(request.user, workspace.pk)
        item = reader.read_unassigned_message(scope, message_id)["message"]
        item["retained_available"] = str(message_id) in retained_capabilities(scope, None, [message_id])
    except reader.CanonicalReadError as exc:
        return _error(exc)
    return render(request, "inbox/unassigned_detail.html", {"workspace": workspace, "item": item})
