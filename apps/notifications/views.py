from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .models import (
    Channel,
    EventType,
    NotificationPreference,
    QuietHours,
)
from .state import apply_snapshot, snapshot, snapshot_for_rows, snapshot_rows, visible_notifications


def _visible(request, *, lock=False):
    return visible_notifications(request.user, getattr(request, "workspace", None), lock_memberships=lock)


def _present(request, notifications):
    for item in notifications:
        item.read_snapshot = snapshot_for_rows(request, [(item.pk, item.revision)])
    return notifications


def _drawer_context(request):
    qs = _visible(request).filter(dismissed_at__isnull=True)
    token = snapshot(request, qs)
    rows = list(
        qs.select_related("inbox_message__social_account", "conversation").order_by("-last_event_at", "-created_at")[
            :50
        ]
    )
    return {"notifications": _present(request, rows), "notification_snapshot": token}


def _history_context(request):
    params = request.POST if request.method == "POST" else request.GET
    kind, status = params.get("event_type", ""), params.get("read_status", "")
    qs = _visible(request)
    token = snapshot(request, qs.filter(dismissed_at__isnull=True))
    qs = qs.filter(dismissed_at__isnull=status != "dismissed")
    if kind:
        qs = qs.filter(event_type=kind)
    if status in {"read", "unread"}:
        qs = qs.filter(is_read=status == "read")
    try:
        page = max(1, int(params.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    total = qs.count()
    page = min(page, max(1, (total + 29) // 30))
    offset = (page - 1) * 30
    rows = list(
        qs.select_related("inbox_message__social_account", "conversation").order_by("-last_event_at", "-created_at")[
            offset : offset + 30
        ]
    )
    return {
        "notifications": _present(request, rows),
        "notification_snapshot": token,
        "event_types": EventType.choices,
        "selected_event_type": kind,
        "selected_read_status": status,
        "page": page,
        "total": total,
        "has_next": total > offset + 30,
        "has_prev": page > 1,
        "update_notification_total": bool(request.htmx),
    }


@login_required
@require_GET
def notification_drawer(request):
    return render(request, "notifications/partials/drawer.html", _drawer_context(request))


@login_required
@require_GET
def notification_list(request):
    return render(
        request,
        "notifications/partials/history_list.html" if request.htmx else "notifications/history.html",
        _history_context(request),
    )


@transaction.atomic
def _mutate(request, *, notification_id=None, dismiss=False):
    qs = _visible(request, lock=True)
    if notification_id:
        qs = qs.filter(pk=notification_id)
    try:
        rows = snapshot_rows(request, request.POST.get("snapshot", ""), qs)
    except signing.BadSignature:
        error = "This notification view has expired or changed. Refresh and try again."
        return (
            HttpResponse(error, status=409) if request.htmx else JsonResponse({"ok": False, "error": error}, status=409)
        )
    updated = apply_snapshot(qs, rows, dismiss=dismiss)
    if not request.htmx:
        return JsonResponse({"ok": True, "updated": updated})
    if request.POST.get("surface") == "history" or request.headers.get("HX-Target") == "notification-history-list":
        response = render(request, "notifications/partials/history_list.html", _history_context(request))
    else:
        response = render(request, "notifications/partials/drawer.html", _drawer_context(request))
    response["HX-Trigger"] = "notificationsChanged"
    return response


@login_required
@require_POST
def mark_as_read(request, notification_id):
    return _mutate(request, notification_id=notification_id)


@login_required
@require_POST
def mark_all_read(request):
    return _mutate(request)


@login_required
@require_POST
def dismiss_notification(request, notification_id):
    return _mutate(request, notification_id=notification_id, dismiss=True)


@login_required
@require_GET
def unread_count(request):
    return JsonResponse({"count": _visible(request).filter(is_read=False, dismissed_at__isnull=True).count()})


@login_required
def preferences(request):
    """Notification preferences page - per event type / channel toggles + quiet hours."""
    if request.method == "POST":
        return _save_preferences(request)

    # Build preference matrix: event_type → channel → enabled
    prefs = NotificationPreference.objects.filter(user=request.user)
    pref_map = {}
    for p in prefs:
        pref_map[(p.event_type, p.channel)] = p.is_enabled

    from .engine import DEFAULT_CHANNELS

    matrix = []
    for event_value, event_label in EventType.choices:
        channel_toggles = []
        for ch_value, ch_label in Channel.choices:
            if (event_value, ch_value) in pref_map:
                enabled = pref_map[(event_value, ch_value)]
            else:
                enabled = DEFAULT_CHANNELS.get(event_value, {}).get(ch_value, False)
            channel_toggles.append(
                {
                    "channel": ch_value,
                    "label": ch_label,
                    "field_name": f"pref_{event_value}_{ch_value}",
                    "enabled": enabled,
                }
            )
        matrix.append(
            {
                "event_type": event_value,
                "event_label": event_label,
                "channel_toggles": channel_toggles,
            }
        )

    quiet_hours, _ = QuietHours.objects.get_or_create(user=request.user)

    context = {
        "matrix": matrix,
        "channel_choices": Channel.choices,
        "quiet_hours": quiet_hours,
    }
    return render(request, "notifications/preferences.html", context)


def _save_preferences(request):
    """Handle POST from preferences form."""
    user = request.user

    # Save channel toggles
    for event_value, _ in EventType.choices:
        for ch_value, _ in Channel.choices:
            field_name = f"pref_{event_value}_{ch_value}"
            is_enabled = field_name in request.POST

            NotificationPreference.objects.update_or_create(
                user=user,
                event_type=event_value,
                channel=ch_value,
                defaults={"is_enabled": is_enabled},
            )

    # Save quiet hours
    quiet_hours, _ = QuietHours.objects.get_or_create(user=user)
    quiet_hours.is_enabled = "quiet_hours_enabled" in request.POST
    from datetime import time as dt_time

    start = request.POST.get("quiet_hours_start", "").strip()
    end = request.POST.get("quiet_hours_end", "").strip()
    if start:
        parts = start.split(":")
        quiet_hours.start_time = dt_time(int(parts[0]), int(parts[1]))
    if end:
        parts = end.split(":")
        quiet_hours.end_time = dt_time(int(parts[0]), int(parts[1]))
    quiet_hours.timezone = request.POST.get("quiet_hours_timezone", "UTC").strip()
    quiet_hours.digest_mode = "digest_mode" in request.POST
    quiet_hours.save()

    from django.contrib import messages

    messages.success(request, "Notification preferences saved.")

    return redirect("notifications:preferences")
