from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    verbose_name = "Notifications"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

        from importlib import import_module

        from .inbox import on_canonical_content_restricted, on_canonical_incoming

        for module_name, signal_name, receiver in [
            ("apps.inbox.conversation_workflow", "canonical_incoming_observed", on_canonical_incoming),
            ("apps.inbox.sync_observations", "canonical_content_restricted", on_canonical_content_restricted),
        ]:
            try:
                module = import_module(module_name)
            except ModuleNotFoundError as exc:
                if exc.name != module_name:
                    raise
            else:
                getattr(module, signal_name).connect(receiver, dispatch_uid=f"notifications.{signal_name}.v1")

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task
        from apps.notifications.tasks import (
            NOTIFICATION_BATCH_INTERVAL_SECONDS,
            NOTIFICATION_RETRY_INTERVAL_SECONDS,
            retry_failed_deliveries,
            send_batched_email_digests,
        )

        register_recurring_task(
            retry_failed_deliveries,
            repeat=NOTIFICATION_RETRY_INTERVAL_SECONDS,
            verbose_name="retry_failed_deliveries",
        )
        register_recurring_task(
            send_batched_email_digests,
            repeat=NOTIFICATION_BATCH_INTERVAL_SECONDS,
            verbose_name="send_batched_email_digests",
        )
