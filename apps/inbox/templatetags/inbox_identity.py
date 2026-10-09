"""Safe sender presentation only; native IDs never become routing evidence."""

from django import template

from apps.inbox.sender_display import sender_display

register = template.Library()


@register.simple_tag
def inbox_sender(record, platform=""):
    return sender_display(record, platform=platform)
