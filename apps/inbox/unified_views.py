"""The unified session shell; detail endpoints retain their existing guards."""

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse

from . import canonical_reads, canonical_views, unified_feed


def feed(request, workspace):
    if any(value.strip() for value in request.GET.getlist("sentiment")):
        return HttpResponse(
            "Sentiment filtering has been retired. Remove the sentiment filter to continue.", status=400
        )
    try:
        selected = unified_feed.filters(request.GET)
        scope = canonical_reads.session_read_scope(request.user, workspace.pk)
        result = unified_feed.read_feed(scope, selected, cursor=request.GET.get("cursor") or None)
        context = unified_feed.filter_context(scope, selected, result["sources"])
    except canonical_reads.CanonicalReadError as exc:
        if exc.code == "invalid_filter":
            return HttpResponse(str(exc), status=422)
        return canonical_views._error(exc)
    params = request.GET.copy()
    for key in ("page", "type", "read_source"):
        params.pop(key, None)
    params["domain"] = selected["domain"]
    params["cursor"] = result["next_cursor"] or ""
    context.update(
        workspace=workspace,
        unified_rows=result["rows"],
        unified_next_url=reverse("inbox:feed", kwargs={"workspace_id": workspace.pk}) + "?" + params.urlencode()
        if result["next_cursor"]
        else "",
        active_domain=selected["domain"],
        inbox_domain=selected["domain"],
        active_account=selected["account"],
        active_platform=selected["platform"],
        active_workflow=selected["workflow"],
        active_status=selected["status"],
        current_view=selected["view"],
        search_query=selected["q"],
        list_cursor=request.GET.get("cursor", ""),
        unavailable_sources=[
            f"{source['account_name']}: saved DMs are unavailable pending identity review. Public messages remain available."
            for source in result["unavailable_sources"]
        ],
        show_legacy_status=selected["domain"] in {"comment", "mention", "review"}
        or selected["domain"] == "dm"
        and bool(result["selected_sources"])
        and all(source["source"] == "legacy" for source in result["selected_sources"]),
        show_canonical_workflow=bool(selected["workflow"])
        or selected["domain"] == "dm"
        and bool(result["selected_sources"])
        and all(source["source"] == "canonical" for source in result["selected_sources"]),
        review_notice="Reviews shows saved records only. Review ingestion is not connected."
        if selected["domain"] == "review"
        else "",
    )
    response = render(
        request,
        "inbox/partials/_unified_list_pane.html"
        if request.htmx and not request.htmx.history_restore_request
        else "inbox/unified_feed.html",
        context,
    )
    try:
        result["guard"]()
    except canonical_reads.CanonicalReadError as exc:
        return canonical_views._error(exc)
    response["Cache-Control"] = "private, no-store"
    return response
