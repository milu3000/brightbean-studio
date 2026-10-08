def unread_notification_count(request):
    """Add unread notification count to all template contexts."""
    if hasattr(request, "user") and request.user.is_authenticated:
        from .state import visible_notifications

        count = (
            visible_notifications(request.user, getattr(request, "workspace", None))
            .filter(is_read=False, dismissed_at__isnull=True)
            .count()
        )
        return {"unread_notification_count": count}
    return {"unread_notification_count": 0}
