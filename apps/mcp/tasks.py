"""Bounded MCP webhook delivery and durable django-background-tasks recovery."""

from datetime import timedelta

from background_task import background
from background_task.tasks import TaskSchedule
from django.db import transaction
from django.utils import timezone

from apps.mcp.event_delivery import CallbackError, post_signed
from apps.mcp.events import _stop, events_enabled, subscription_authorized
from apps.mcp.models import EventOutbox, EventSubscription

MAX_ATTEMPTS = 8
SWEEP_INTERVAL_SECONDS = 60
TRANSIENT_REASONS = {"timeout", "connection_failed", "dns_failed"}


def process_delivery(delivery_id):
    """At-least-once send, serialized with unsubscribe and concurrent workers.

    Hold a short DB row lock during the bounded network call. A worker crash
    rolls back its claim and the sweep retries the SAME id/body. A receiver must
    deduplicate webhook-id; no sender can guarantee exactly-once HTTP delivery.
    Lock the subscription before its outbox to keep unsubscribe's order stable.
    """
    if not events_enabled():
        return
    sub_id = EventOutbox.objects.filter(pk=delivery_id).values_list("subscription_id", flat=True).first()
    if sub_id is None:
        return
    with transaction.atomic():
        sub = EventSubscription.objects.select_for_update().filter(pk=sub_id).first()
        if sub is None:
            return
        delivery = EventOutbox.objects.select_for_update().select_related("message").filter(pk=delivery_id).first()
        now = timezone.now()
        if delivery is None or delivery.status != EventOutbox.Status.PENDING or delivery.next_attempt_at > now:
            return
        if not sub.active or sub.generation != delivery.generation:
            delivery.status = EventOutbox.Status.CANCELLED
            delivery.last_error = "subscription_inactive"
            delivery.save(update_fields=["status", "last_error"])
            return
        if sub.expires_at <= now or not subscription_authorized(sub):
            _stop(sub, "expired" if sub.expires_at <= now else "access_revoked")
            return
        message = delivery.message
        if (
            message.workspace_id != sub.workspace_id
            or message.social_account_id != sub.social_account_id
            or message.message_type != "dm"
        ):
            delivery.status = EventOutbox.Status.CANCELLED
            delivery.last_error = "message_scope_changed"
            delivery.save(update_fields=["status", "last_error"])
            return
        delivery.attempts += 1
        previous_secret = sub.previous_secret if sub.previous_secret_until and sub.previous_secret_until > now else ""
        try:
            response = post_signed(
                sub.callback_url,
                sub.signing_secret,
                sub.id,
                delivery.event_id,
                delivery.payload.encode("utf-8"),
                previous_secret=previous_secret,
            )
            delivery.last_status = response.status
            delivery.last_error = "" if 200 <= response.status < 300 else "http_error"
            accepted = 200 <= response.status < 300
            transient = response.status in (408, 425, 429) or 500 <= response.status < 600
            if response.status == 410:
                _stop(sub, "receiver_gone")
                delivery.status = EventOutbox.Status.CANCELLED
        except CallbackError as exc:
            delivery.last_status = None
            delivery.last_error = exc.reason
            accepted = False
            transient = exc.reason in TRANSIENT_REASONS
        if accepted:
            delivery.status = EventOutbox.Status.DELIVERED
            delivery.delivered_at = timezone.now()
        elif delivery.status != EventOutbox.Status.CANCELLED:
            if transient and delivery.attempts < MAX_ATTEMPTS:
                delivery.next_attempt_at = timezone.now() + timedelta(
                    seconds=min(30 * 2 ** (delivery.attempts - 1), 3600)
                )
            else:
                delivery.status = EventOutbox.Status.FAILED
        delivery.save(
            update_fields=["attempts", "status", "next_attempt_at", "delivered_at", "last_status", "last_error"]
        )
        # No immediate retry task is needed: the persisted due time is claimed
        # by the recurring recovery sweep, including after server restarts.


@background(schedule={"action": TaskSchedule.CHECK_EXISTING})
def deliver_event(delivery_id):
    process_delivery(delivery_id)


@background(schedule=0)
def recover_event_outbox():
    if not events_enabled():
        return
    # Expiry and old-key cleanup run even when no messages arrive.
    expired = EventSubscription.objects.filter(active=True, expires_at__lte=timezone.now()).values_list(
        "pk", flat=True
    )[:100]
    for subscription_id in expired:
        with transaction.atomic():
            sub = EventSubscription.objects.select_for_update().filter(pk=subscription_id).first()
            if sub and sub.active and sub.expires_at <= timezone.now():
                _stop(sub, "expired")
    EventSubscription.objects.filter(previous_secret_until__lte=timezone.now()).update(
        previous_secret="", previous_secret_until=None
    )
    ids = (
        EventOutbox.objects.filter(status=EventOutbox.Status.PENDING, next_attempt_at__lte=timezone.now())
        .order_by("next_attempt_at")
        .values_list("id", flat=True)[:100]
    )
    for delivery_id in ids:
        deliver_event(str(delivery_id))
