"""Conservative, read-time DM grouping. Stored messages and API identities stay intact.

The first iteration deliberately has no conversation-wide mutations: status,
assignment and SLA remain attached to individual messages. Metadata is scanned
before pagination, so one busy contact cannot consume every list slot. Only the
selected page's message bodies are loaded. No provider calls or backfill writes.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.core.paginator import Paginator
from django.db.models import F, Max
from django.db.models.functions import Coalesce

from .models import InboxMessage, InboxReply


@dataclass
class InboxEntry:
    message: InboxMessage
    count: int = 1
    unread_count: int = 0
    pending_count: int = 0
    status: str = "open"
    last_activity: datetime | None = None

    @property
    def is_conversation(self):
        return self.message.message_type == InboxMessage.MessageType.DM

    @property
    def status_label(self):
        return {"unread": "Unread", "open": "Needs attention", "resolved": "Resolved", "archived": "Archived"}[
            self.status
        ]


def _identifier(value):
    # Booleans, lists and JSON objects must never become shared identity keys.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def _identity(row):
    native = _identifier(row["native_id"])
    # JSON key extraction keeps potentially large raw message payloads out of
    # the index. Conflicting scoped IDs are not a safe fallback identity.
    remotes = {_identifier(row["scoped_id"]), _identifier(row["webhook_sender"])}
    remotes.discard("")
    remote = next(iter(remotes)) if len(remotes) == 1 else ""
    # Only the known one-to-one Meta DM integrations may use a peer fallback.
    if row["social_account__platform"] not in {"facebook", "instagram", "instagram_login"}:
        remote = ""
    return native, remote


class ConversationIndex:
    def __init__(self, queryset):
        self.rows = list(
            queryset.filter(message_type=InboxMessage.MessageType.DM)
            .order_by("received_at", "id")
            .annotate(
                native_id=F("extra__conversation_id"),
                scoped_id=F("extra__sender_id"),
                webhook_sender=F("extra__sender__id"),
            )
            .values(
                "id",
                "social_account_id",
                "status",
                "received_at",
                "social_account__platform",
                "native_id",
                "scoped_id",
                "webhook_sender",
            )
        )
        identities = {row["id"]: _identity(row) for row in self.rows}
        native_by_sender: dict[tuple, set] = defaultdict(set)
        native_senders: dict[tuple, set] = defaultdict(set)
        for row in self.rows:
            native, sender = identities[row["id"]]
            if native and sender:
                native_by_sender[(row["social_account_id"], sender)].add(native)
                native_senders[(row["social_account_id"], native)].add(sender)
        self.groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        self.keys = {}
        for row in self.rows:
            native, sender = identities[row["id"]]
            account = row["social_account_id"]
            candidates = native_by_sender.get((account, sender), set())
            if native:
                key = (account, "native", native)
            elif sender and len(candidates) == 1 and len(native_senders[(account, next(iter(candidates)))]) == 1:
                key = (account, "native", next(iter(candidates)))
            elif sender and not candidates:
                key = (account, "sender", sender)
            else:
                # Missing identity or multiple native threads: fail closed.
                key = (account, "message", row["id"])
            self.groups[key].append(row)
            self.keys[row["id"]] = key

    def members(self, message_id):
        key = self.keys.get(message_id)
        return self.groups.get(key, [])


def entry_for(message, members, last_activity=None):
    statuses = [row["status"] for row in members] or [message.status]
    status = next((s for s in ("unread", "open", "resolved", "archived") if s in statuses), "open")
    return InboxEntry(
        message=message,
        count=len(statuses),
        unread_count=statuses.count("unread"),
        pending_count=sum(s in ("unread", "open") for s in statuses),
        status=status,
        last_activity=last_activity or message.received_at,
    )


def inbox_page(base, filtered, page_number=1, page_size=50):
    """Select matching conversations first; paginate groups, never raw messages.

    Filters match any member; the displayed preview/status uses the whole group.
    Permission/workspace scoping must already be applied to both querysets.
    """
    index = ConversationIndex(base)
    matching = set(filtered.values_list("id", flat=True))
    candidates = []
    members_by_id = {}
    activity_by_key = {}
    latest_replies = (
        InboxReply.objects.filter(inbox_message__in=base.filter(message_type="dm"), status=InboxReply.Status.SENT)
        .values("inbox_message_id")
        .annotate(latest=Max(Coalesce("sent_at", "created_at")))
    )
    for reply in latest_replies:
        key = index.keys.get(reply["inbox_message_id"])
        if key is not None:
            previous = activity_by_key.get(key)
            activity_by_key[key] = max(previous, reply["latest"]) if previous else reply["latest"]
    for key, members in index.groups.items():
        if any(row["id"] in matching for row in members):
            latest = members[-1]
            activity = max(latest["received_at"], activity_by_key.get(key, latest["received_at"]))
            candidates.append((activity, str(latest["id"]), latest["id"]))
            members_by_id[latest["id"]] = members
    candidates.extend(
        (received, str(pk), pk) for pk, received in filtered.exclude(message_type="dm").values_list("id", "received_at")
    )
    candidates.sort(reverse=True)
    page = Paginator(candidates, page_size).get_page(page_number)
    objects = base.filter(id__in=[row[2] for row in page]).select_related("social_account", "assigned_to").in_bulk()
    entries = [
        entry_for(objects[row[2]], members_by_id.get(row[2], []), last_activity=row[0])
        for row in page
        if row[2] in objects
    ]
    return entries, page
