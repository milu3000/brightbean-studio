"""Public comment identity and mention facets, separate from private threads."""

from django.db.models import BooleanField, F, Func, Q

from .models import InboxMessage

PUBLIC_TYPES = {"comment", "mention"}


class _TrueMention(Func):
    arity = 1
    output_field = BooleanField()

    def as_sqlite(self, compiler, connection, **extra_context):
        sql, params = compiler.compile(self.source_expressions[0])
        return f"COALESCE(JSON_TYPE({sql}, '$.is_mention') = 'true', 0)", params

    def as_postgresql(self, compiler, connection, **extra_context):
        sql, params = compiler.compile(self.source_expressions[0])
        return f"COALESCE(({sql} -> 'is_mention') = 'true'::jsonb, false)", params


def public_native_id(value):
    if not isinstance(value, str) or not value or len(value) > 255:
        return ""
    return "" if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) else value


def public_type_filter(value):
    """A mention is a facet of a comment, never a duplicated stored event."""
    if value == "mention":
        marked = (
            InboxMessage.objects.filter(message_type="comment")
            .alias(mention_marker=_TrueMention(F("extra")))
            .filter(mention_marker=True)
            .values("pk")
        )
        return Q(message_type="mention") | Q(pk__in=marked)
    if value == "comment":
        return Q(message_type="comment") | Q(message_type="mention", extra__reply_edge="comment")
    return Q(message_type=value)


def public_post_id(message):
    extra = message.extra if isinstance(message.extra, dict) else {}
    # Preserve the full platform ID. A stripped Facebook post suffix is not an
    # independent proof that two public events belong to the same post.
    return public_native_id(extra.get("post_id")) or public_native_id(extra.get("media_id"))


def public_thread_key(message, *, parents=None):
    """Return a proved post/root key, or an exact-row fallback when incomplete.

    Arbitrary thread/root hints and parent FK links are not provider evidence.
    Every parent must belong to the exact account, workspace and full post.
    Missing parents, cycles and missing root evidence remain separate rows.
    """
    if message.message_type not in PUBLIC_TYPES:
        return None
    prefix = (str(message.workspace_id), str(message.social_account_id), "public")
    fallback = (*prefix, "message", str(message.pk))
    post = public_post_id(message)
    if not post:
        return fallback
    parents = {} if parents is None else parents
    current, seen = message, set()
    for _ in range(50):
        mid = public_native_id(current.platform_message_id)
        if not mid or mid in seen or public_post_id(current) != post:
            return fallback
        seen.add(mid)
        extra = current.extra if isinstance(current.extra, dict) else {}
        if "parent_id" not in extra:
            return fallback
        parent = extra.get("parent_id")
        if parent == "" or parent == post:
            return (*prefix, post, mid)
        parent = public_native_id(parent)
        if not parent:
            return fallback
        lookup = (str(message.workspace_id), str(message.social_account_id), parent)
        if lookup not in parents:
            parents[lookup] = (
                InboxMessage.objects.filter(
                    workspace_id=message.workspace_id,
                    social_account_id=message.social_account_id,
                    social_account__workspace_id=message.workspace_id,
                    platform_message_id=parent,
                    message_type__in=PUBLIC_TYPES,
                )
                .only("id", "workspace_id", "social_account_id", "platform_message_id", "message_type", "extra")
                .first()
            )
        current = parents[lookup]
        if current is None:
            return fallback
    return fallback


def public_upsert_defaults(existing, defaults):
    """Merge public evidence without downgrading a comment or losing a mention.

    Called while the shared account lock is held. It never creates a second
    event, overwrites private messages, or replaces real text with a pointer.
    """
    values = dict(defaults)
    old = existing.extra if existing is not None and isinstance(existing.extra, dict) else {}
    incoming = values.get("extra") if isinstance(values.get("extra"), dict) else {}
    extra = {**old, **incoming}
    if values["message_type"] == "mention" or (existing is not None and existing.message_type == "mention"):
        extra["is_mention"] = True
    if old.get("is_mention") is True or incoming.get("is_mention") is True:
        extra["is_mention"] = True
    if extra.get("is_mention") is not True:
        extra.pop("is_mention", None)
    if existing is not None:
        if existing.message_type == "comment":
            values["message_type"] = "comment"
        placeholder = values.get("body") == "You were mentioned on Instagram. Open the post to read the mention."
        editing = incoming.get("verb") in {"edit", "edited"}
        if existing.body and ((not values.get("body") and not editing) or placeholder):
            values["body"] = existing.body
        for field in ("sender_name", "sender_handle", "sender_avatar_url"):
            if not values.get(field) or values.get(field) in {"Instagram", "Unknown"}:
                values[field] = getattr(existing, field)
        # Polling/metadata enrichment is not a new event or a new reply window.
        values.pop("received_at", None)
    values["extra"] = extra
    return values
