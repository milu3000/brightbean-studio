"""Read-only conversation views over the inbox records already stored here.

Native conversation IDs are opaque and account scoped. Missing or malformed
IDs never fall back to a sender, handle, participant list or invented thread.
This module does not enroll capture, call a provider or write history.
"""

from dataclasses import dataclass, field

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import CharField, F, Func, Value
from django.db.models.functions import Coalesce

from .models import InboxMessage, InboxReply, InternalNote


class _NativeThreadId(Func):
    """Extract a string only, keeping opaque numeric-looking IDs intact.

    SQLite's JSON key converter otherwise conflates strings with JSON scalars.
    Both supported inbox databases must agree about the exact identity type.
    """

    arity = 1
    output_field = CharField()

    def as_postgresql(self, compiler, connection, **extra_context):
        sql, params = compiler.compile(self.source_expressions[0])
        return (
            f"CASE WHEN jsonb_typeof({sql} -> 'conversation_id') = 'string' "
            f"THEN {sql} ->> 'conversation_id' ELSE NULL END",
            params * 2,
        )

    def as_sqlite(self, compiler, connection, **extra_context):
        sql, params = compiler.compile(self.source_expressions[0])
        return (
            f"CASE WHEN JSON_TYPE({sql}, '$.conversation_id') = 'text' "
            f"THEN JSON_EXTRACT({sql}, '$.conversation_id') ELSE NULL END",
            params * 2,
        )


def enabled():
    return getattr(settings, "INBOX_CONVERSATION_PRESENTATION_ENABLED", True) is True


def native_thread_id(value):
    """Accept only complete opaque string IDs, without normalization."""
    if not isinstance(value, str) or not value or len(value) > 255:
        return ""
    return "" if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) else value


def thread_id(message):
    extra = message.extra if isinstance(message.extra, dict) else {}
    return native_thread_id(extra.get("conversation_id")) if message.message_type == InboxMessage.MessageType.DM else ""


def stored_thread_messages(message):
    """Never broaden an ID beyond the selected row's exact workspace/account."""
    qs = InboxMessage.objects.filter(
        workspace_id=message.workspace_id,
        social_account_id=message.social_account_id,
        social_account__workspace_id=message.workspace_id,
    )
    native_id = thread_id(message)
    if native_id:
        return qs.annotate(presentation_native_id=_NativeThreadId(F("extra"))).filter(
            message_type=InboxMessage.MessageType.DM, presentation_native_id=native_id
        )
    return qs.filter(pk=message.pk)


@dataclass
class InboxRow:
    message_id: object
    message: object = None
    matched_count: int = 0
    unread_count: int = 0
    open_count: int = 0
    assignments: set = field(default_factory=set)

    @property
    def mixed_assignments(self):
        return len(self.assignments) > 1


def inbox_page(queryset, page_number, *, per_page=50):
    """Group matching records before paging, without loading message bodies.

    Filters apply to original records. The newest matching record is the selected
    message, and bulk actions continue to affect only that explicit original ID.
    Streaming lightweight metadata avoids a query per row or loading media bodies.
    """
    groups = {}
    columns = (
        "pk",
        "workspace_id",
        "social_account_id",
        "message_type",
        "presentation_native_id",
        "status",
        "assigned_to_id",
    )
    metadata = queryset.annotate(presentation_native_id=_NativeThreadId(F("extra")))
    for record in metadata.order_by("-received_at", "-pk").values(*columns).iterator(chunk_size=1000):
        native_id = (
            native_thread_id(record["presentation_native_id"])
            if enabled() and record["message_type"] == InboxMessage.MessageType.DM
            else ""
        )
        key = (record["workspace_id"], record["social_account_id"], native_id) if native_id else (record["pk"],)
        row = groups.setdefault(key, InboxRow(message_id=record["pk"]))
        row.matched_count += 1
        row.unread_count += record["status"] == InboxMessage.Status.UNREAD
        row.open_count += record["status"] == InboxMessage.Status.OPEN
        row.assignments.add(record["assigned_to_id"])
    page = Paginator(list(groups.values()), per_page).get_page(page_number)
    records = queryset.select_related("social_account", "assigned_to").in_bulk(
        [row.message_id for row in page.object_list]
    )
    page.object_list = [row for row in page.object_list if row.message_id in records]
    for row in page.object_list:
        row.message = records[row.message_id]
    return page


def timeline_page(messages, page_number, *, per_page=50):
    """Page all stored inbound, sent replies and internal notes by event time.

    Page one shows the latest events in reading order. Drafts and uncertain sends
    are pending work and are never represented as delivered timeline events.
    """
    ids = messages.values("pk")
    incoming = (
        messages.order_by()
        .annotate(event_kind=Value("incoming", output_field=CharField()), event_at=F("received_at"), event_id=F("pk"))
        .values("event_kind", "event_at", "event_id")
    )
    replies = (
        InboxReply.objects.filter(inbox_message_id__in=ids, status=InboxReply.Status.SENT)
        .order_by()
        .annotate(
            event_kind=Value("reply", output_field=CharField()),
            event_at=Coalesce("sent_at", "created_at"),
            event_id=F("pk"),
        )
        .values("event_kind", "event_at", "event_id")
    )
    notes = (
        InternalNote.objects.filter(inbox_message_id__in=ids)
        .order_by()
        .annotate(event_kind=Value("note", output_field=CharField()), event_at=F("created_at"), event_id=F("pk"))
        .values("event_kind", "event_at", "event_id")
    )
    events = incoming.union(replies, notes, all=True).order_by("-event_at", "-event_kind", "-event_id")
    page = Paginator(events, per_page).get_page(page_number)
    references = list(page.object_list)
    models = {
        "incoming": messages.select_related("social_account", "assigned_to"),
        "reply": InboxReply.objects.filter(inbox_message_id__in=ids).select_related("author", "inbox_message"),
        "note": InternalNote.objects.filter(inbox_message_id__in=ids).select_related("author", "inbox_message"),
    }
    objects = {
        kind: qs.in_bulk([event["event_id"] for event in references if event["event_kind"] == kind])
        for kind, qs in models.items()
    }
    page.object_list = [
        (event["event_kind"], objects[event["event_kind"]][event["event_id"]], event["event_at"])
        for event in reversed(references)
        if event["event_id"] in objects[event["event_kind"]]
    ]
    return page
