"""Local test URLconf only. No workspace_id URL kwarg or legacy detail view."""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.inbox.conversation_read import InvalidTimelineCursorError, read_work_timeline

from .fixtures import SCENARIO_KEYS, SCENARIOS, synthetic_id


class PreviewHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Cache-Control"] = "private, no-store"
        response["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        response["Referrer-Policy"] = "no-referrer"
        return response


@require_GET
@login_required
def index(request):
    return redirect("preview_timeline", scenario="burst")


@require_GET
@login_required
def timeline(request, scenario):
    if not getattr(settings, "SYNTHETIC_TIMELINE_PREVIEW", False) or scenario not in SCENARIO_KEYS:
        raise PermissionDenied
    navigation = [
        {"key": key, "label": label, "description": description, "url": reverse("preview_timeline", args=[key])}
        for key, label, description in SCENARIOS
    ]
    current = next(item for item in navigation if item["key"] == scenario)
    try:
        data = read_work_timeline(
            request.user, synthetic_id("workspace"), synthetic_id("work-" + scenario), cursor=request.GET.get("cursor")
        )
    except InvalidTimelineCursorError as exc:
        return render(request, "preview_error.html", {"error": str(exc), "first_page": current["url"]}, status=400)
    return render(request, "preview_timeline.html", {**data, "navigation": navigation, "current": current})
