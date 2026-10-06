"""Explicit local post-status annotations, never remote archive/delete calls."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.composer.models import PlatformPost
from apps.members.models import WorkspaceMembership

from . import services
from .status import confirm_post_availability
from .views import _get_workspace


@login_required
@require_POST
def confirm_post_status(request, workspace_id, post_id):
    workspace = _get_workspace(request, workspace_id)
    membership = WorkspaceMembership.objects.get(user=request.user, workspace=workspace)
    if not membership.effective_permissions.get("create_posts", False):
        raise PermissionDenied("Permission denied: create_posts")
    post = get_object_or_404(
        PlatformPost.objects.select_related("social_account", "post"),
        pk=post_id,
        social_account__workspace=workspace,
    )
    error = ""
    try:
        confirm_post_availability(
            post,
            availability=request.POST.get("availability"),
            expected_version=int(request.POST.get("expected_version", "")),
            confirmed=request.POST.get("confirmed") == "yes",
        )
    except (ValueError, TypeError) as exc:
        error = str(exc)
        post.refresh_from_db()
    if not request.headers.get("HX-Request"):
        if error:
            messages.error(request, error)
        else:
            messages.success(request, "Local post annotation updated.")
        return redirect("analytics:account", workspace_id=workspace.pk, account_id=post.social_account_id)
    context = services.post_detail(post)
    context.update(workspace=workspace, can_confirm_status=True, status_confirmation_error=error)
    response = render(request, "analytics/_post_detail.html", context)
    if not error:
        response["HX-Trigger"] = "analyticsStatusChanged"
    return response
