from django.apps import AppConfig


class McpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mcp"
    # ``mcp`` is also the name of a popular PyPI package; pick a unique
    # label so Django's app registry can't collide with one in the future.
    label = "mcp_server"
    verbose_name = "Model Context Protocol Server"

    def ready(self):
        # Force registration of all tools at app boot so `tools/list`
        # returns a complete catalog regardless of which router is hit
        # first. Import-side-effects only.
        from django.db.models.signals import post_migrate

        from apps.mcp import conversation_tools, handlers, reply_coordination_tools  # noqa: F401

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task
        from apps.mcp.tasks import SWEEP_INTERVAL_SECONDS, recover_event_outbox

        register_recurring_task(
            recover_event_outbox,
            repeat=SWEEP_INTERVAL_SECONDS,
            verbose_name="recover_mcp_event_outbox",
        )
