"""One read-only feed over disjoint, currently authorized inbox sources.

Group legacy rows before globally ordering them with canonical conversations.
The signed keyset is bound to the complete metadata snapshot: a changed source,
identity, grant or ordering rejects a stale page instead of dropping/repeating
items. No saved data is repaired and no provider is contacted by this reader.
"""

from datetime import UTC, datetime
from urllib.parse import urlencode

from django.core import signing
from django.db.models import Exists, OuterRef, Q, Subquery, TextField
from django.db.models.functions import MD5, Cast
from django.urls import reverse

from apps.social_accounts.models import SocialAccount

from . import canonical_reads as reader
from . import presentation
from .canonical_access import digest, narrow_scope
from .canonical_compat import inbox_source_snapshot, legacy_body_search_query
from .canonical_send_target import canonical_projection_view, exclude_transport_projections
from .models import ConversationMessage, InboxConversation, InboxMessage
from .public_threads import PUBLIC_TYPES, public_native_id, public_post_id, public_thread_key, public_type_filter

SALT = "inbox.unified-feed.v1"
DOMAINS = (("all", "All Types"), ("dm", "DMs"), ("comment", "Comments"), ("mention", "Mentions"), ("review", "Reviews"))
# Implemented local provider/webhook paths, not claims about platform APIs.
CAPABILITIES = {
    "facebook": {"dm", "comment", "mention"},
    "instagram": {"comment", "mention"},
    "instagram_login": {"dm", "comment", "mention"},
    "youtube": {"comment"},
    "linkedin": {"comment"},
    "mastodon": {"mention"},
}


def _invalid(message="This saved filter is unavailable here. Choose filters from the inbox."):
    return reader.CanonicalReadError("invalid_filter", message)


def filters(parameters):
    keys = ("q", "account", "platform", "workflow", "status", "cursor", "domain", "type", "view")
    if any(len(parameters.getlist(key)) > 1 for key in keys):
        raise _invalid("Choose one value per inbox filter.")
    domain = parameters.get("domain") or parameters.get("type") or "all"
    if domain not in dict(DOMAINS):
        raise _invalid("Unknown inbox section.")
    if parameters.get("type") and parameters.get("type") not in {domain, ""}:
        raise _invalid()
    if any(parameters.get(key) for key in ("assigned", "date_from", "date_to", "sentiment")):
        raise _invalid()
    view = parameters.get("view") or "all"
    if view not in {"all", "mine", "unassigned"} or (view != "all" and domain == "all"):
        raise _invalid("Queue filters apply to public messages or a legacy-only DM selection.")
    search = parameters.get("q", "").strip()
    if len(search) > 500:
        raise _invalid("Search must be at most 500 characters.")
    status, workflow = parameters.get("status", ""), parameters.get("workflow", "")
    if status and workflow:
        raise _invalid("Choose message status or DM workflow, not both.")
    if status and (domain not in {"dm", "comment", "mention", "review"} or status not in InboxMessage.Status.values):
        raise _invalid("Message status applies to saved messages; canonical DMs use workflow.")
    if workflow and (domain != "dm" or workflow not in {"needs_action", "waiting", "done", "unclassified"}):
        raise _invalid("Conversation workflow applies to saved DMs.")
    platform = parameters.get("platform", "")
    if platform and platform not in dict(SocialAccount._meta.get_field("platform").choices):
        raise _invalid("Unknown inbox platform.")
    return {
        "domain": domain,
        "account": parameters.get("account", ""),
        "platform": platform,
        "q": search,
        "status": status,
        "workflow": workflow,
        "view": view,
    }


def _legacy_query(scope, sources, selected):
    query = exclude_transport_projections(
        InboxMessage.objects.filter(
            workspace_id=scope.workspace_id,
            social_account__workspace_id=scope.workspace_id,
            social_account_id__in=[item["id"] for item in sources],
        )
    ).exclude(message_type="dm", social_account_id__in=[item["id"] for item in sources if item["source"] != "legacy"])
    if selected["domain"] != "all":
        query = query.filter(public_type_filter(selected["domain"]))
    if selected["status"]:
        query = query.filter(status=selected["status"])
    if selected.get("view") == "mine":
        query = query.filter(assigned_to_id=scope.user_id)
    elif selected.get("view") == "unassigned":
        query = query.filter(assigned_to__isnull=True)
    if selected["workflow"]:
        query = query.none()
    if selected["q"]:
        search = selected["q"]
        query = query.filter(
            legacy_body_search_query(search) | Q(sender_name__icontains=search) | Q(sender_handle__icontains=search)
        )
    return query


def _public_parent_cache(messages):
    """Batch exact ancestor metadata; never fetch parent content or peers.

    Filters can omit a proving parent. Resolve those identities outside the
    filter, but only inside the matching rows' exact workspace/account pairs.
    Rebuild on each metadata pass so a changed parent still invalidates cursors.
    """
    parents = {
        (str(message.workspace_id), str(message.social_account_id), message.platform_message_id): message
        for message in messages
        if message.message_type in PUBLIC_TYPES and public_native_id(message.platform_message_id)
    }
    pending = list(parents.values())
    for _ in range(50):
        missing = set()
        for message in pending:
            extra = message.extra if isinstance(message.extra, dict) else {}
            parent = public_native_id(extra.get("parent_id"))
            if parent and parent != public_post_id(message):
                key = (str(message.workspace_id), str(message.social_account_id), parent)
                if key not in parents:
                    missing.add(key)
        if not missing:
            break
        grouped = {}
        for workspace_id, account_id, native_id in missing:
            grouped.setdefault((workspace_id, account_id), []).append(native_id)
        condition = Q(pk__in=[])
        for (workspace_id, account_id), native_ids in grouped.items():
            condition |= Q(
                workspace_id=workspace_id,
                social_account_id=account_id,
                social_account__workspace_id=workspace_id,
                platform_message_id__in=native_ids,
            )
        pending = list(
            InboxMessage.objects.filter(condition, message_type__in=PUBLIC_TYPES).only(
                "id", "workspace_id", "social_account_id", "platform_message_id", "message_type", "extra"
            )
        )
        parents.update(dict.fromkeys(missing))
        for message in pending:
            parents[(str(message.workspace_id), str(message.social_account_id), message.platform_message_id)] = message
    return parents


def _legacy_metadata(query):
    fields = (
        "pk",
        "workspace_id",
        "social_account_id",
        "message_type",
        "platform_message_id",
        "extra",
        "status",
        "assigned_to_id",
        "received_at",
        "sender_name",
        "sender_handle",
        "body_digest",
    )
    shadows = ConversationMessage.objects.filter(
        Q(legacy_message_id=OuterRef("pk"))
        | Q(social_account_id=OuterRef("social_account_id"), platform_message_id=OuterRef("platform_message_id"))
    )
    records = list(query.annotate(body_digest=MD5("body"), has_shadow=Exists(shadows)).values(*fields, "has_shadow"))
    # A pre-durable legacy DM may already have an exact canonical shadow. Use
    # the same guarded projection's timestamp before grouping and ordering,
    # rather than showing a different time from the keyset's ordering evidence.
    for record in records:
        if record["message_type"] == "dm" and record["has_shadow"]:
            message = InboxMessage(
                **{key: value for key, value in record.items() if key not in {"body_digest", "has_shadow"}}
            )
            record["received_at"] = canonical_projection_view(message).received_at
    records.sort(key=lambda record: (record["received_at"], str(record["pk"])), reverse=True)
    messages = [
        InboxMessage(**{key: value for key, value in record.items() if key not in {"body_digest", "has_shadow"}})
        for record in records
    ]
    parents = _public_parent_cache(messages) if presentation.enabled() else {}
    groups, membership = {}, []
    for record, message in zip(records, messages, strict=True):
        native = presentation.thread_id(message) if presentation.enabled() else ""
        key = (record["workspace_id"], record["social_account_id"], "dm", native) if native else (record["pk"],)
        if presentation.enabled() and record["message_type"] in PUBLIC_TYPES:
            key = public_thread_key(message, parents=parents)
        membership.append((record["pk"], key))
        if key not in groups:
            groups[key] = {
                "id": str(record["pk"]),
                "source": "legacy",
                "stamp": record["received_at"],
                "unread": False,
                "matched_count": 0,
            }
        groups[key]["unread"] |= record["status"] == "unread"
        groups[key]["matched_count"] += 1
    # A nonmatching public parent can change the grouping proof even though
    # every filtered record is unchanged. Bind each exact row-to-group mapping.
    return list(groups.values()), digest([records, membership])


def _canonical_query(scope, selected, accounts):
    query = InboxConversation.objects.filter(reader._scope_filter(scope, accounts))
    messages = reader._message_subquery(scope, accounts)
    query = query.annotate(has_messages=Exists(messages)).filter(has_messages=True)
    if selected["workflow"]:
        query = query.filter(workflow_state=None if selected["workflow"] == "unclassified" else selected["workflow"])
    if selected["q"]:
        search = selected["q"]
        matching = messages.filter(reader._visible_content_query()).filter(
            Q(body__icontains=search) | Q(direction="inbound", sender_name__icontains=search)
        )
        query = query.annotate(has_match=Exists(matching)).filter(
            Q(has_match=True)
            | Q(peer_id__icontains=search)
            | Q(social_account__account_name__icontains=search)
            | Q(social_account__account_handle__icontains=search)
        )
    return query.annotate(
        latest_at=Subquery(
            messages.filter(occurred_at__isnull=False).order_by("-occurred_at", "-pk").values("occurred_at")[:1]
        )
    )


def _canonical_metadata(query):
    records = list(
        query.order_by("pk").values(
            "pk",
            "workspace_id",
            "social_account_id",
            "platform",
            "platform_conversation_id",
            "peer_id",
            "peer_ambiguous",
            "conversation_type",
            "classification_reason",
            "revision",
            "incoming_generation",
            "workflow_state",
            "latest_at",
        )
    )
    return [{"id": str(row["pk"]), "source": "canonical", "stamp": row["latest_at"]} for row in records], digest(
        records
    )


def _content_stamp(scope, sources):
    """Bind default-content restrictions without loading retained/archive data.

    Source enrollment and conversation revisions do not necessarily change when
    a saved sidecar is withdrawn or an unlinked native shadow is corrected.
    Hash default content in the DB; never copy it into the signed cursor.
    """
    readable_ids = [source["id"] for source in sources if source["source"] != "canonical_held"]
    messages = ConversationMessage.objects.filter(
        workspace_id=scope.workspace_id,
        social_account__workspace_id=scope.workspace_id,
        social_account_id__in=readable_ids,
    ).annotate(body_digest=MD5("body"), attachment_digest=MD5(Cast("attachments", TextField())))
    state = list(
        messages.order_by("pk").values(
            "pk",
            "workspace_id",
            "social_account_id",
            "platform",
            "conversation_id",
            "platform_message_id",
            "legacy_message_id",
            "legacy_reply_id",
            "sender_id",
            "sender_name",
            "recipient_id",
            "direction",
            "occurred_at",
            "body_digest",
            "attachment_digest",
            "content_status",
            "is_deleted",
            "conversation_type",
            "classification_reason",
            "delivery_status",
            "conversation__workspace_id",
            "conversation__social_account_id",
            "conversation__platform",
            "conversation__platform_conversation_id",
            "conversation__peer_id",
            "conversation__peer_ambiguous",
            "conversation__conversation_type",
            "conversation__classification_reason",
            "legacy_reply__conversation_id",
            "legacy_reply__inbox_message__workspace_id",
            "legacy_reply__inbox_message__social_account_id",
            "legacy_reply__inbox_message__platform_message_id",
            "legacy_reply__inbox_message__message_type",
            "conversation__revision",
            "conversation__sync_identity__connection_id",
            "conversation__sync_identity__connection_generation",
            "observation_state__connection_generation",
            "observation_state__withdrawn_at",
            "observation_state__expired_at",
        )
    )
    # Native-ID (including unlinked) legacy restrictions remain authoritative.
    anchors = (
        InboxMessage.objects.filter(
            workspace_id=scope.workspace_id,
            social_account__workspace_id=scope.workspace_id,
            social_account_id__in=readable_ids,
            message_type="dm",
        )
        .annotate(extra_digest=MD5(Cast("extra", TextField())))
        .order_by("pk")
        .values(
            "pk",
            "workspace_id",
            "social_account_id",
            "platform_message_id",
            "received_at",
            "extra_digest",
        )
    )
    return digest([state, list(anchors)])


def _key(row):
    return (row["stamp"] or datetime.min.replace(tzinfo=UTC), row["source"], row["id"])


def _position(row):
    return [row["stamp"].isoformat() if row["stamp"] else None, row["source"], row["id"]]


def _page(metadata, binding, cursor, limit):
    ordered = sorted(metadata, key=_key, reverse=True)
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError
            value = signing.loads(cursor, salt=SALT, max_age=3600)
            if value["scope"] != binding:
                raise ValueError
            # The exact boundary must still exist in the bound snapshot.
            boundary = next(index for index, row in enumerate(ordered) if _position(row) == value["after"])
            ordered = ordered[boundary + 1 :]
        except (signing.BadSignature, KeyError, TypeError, ValueError, StopIteration) as exc:
            raise reader.CanonicalReadError("stale_cursor", "The inbox changed or this page expired; reload.") from exc
    rows = ordered[:limit]
    token = (
        signing.dumps({"scope": binding, "after": _position(rows[-1])}, salt=SALT, compress=True)
        if len(ordered) > limit
        else None
    )
    return rows, token


def _project_legacy(message, metadata, workspace_id):
    from .sender_display import sender_display

    message = canonical_projection_view(message)
    sender = sender_display(message, platform=message.social_account.platform)
    unavailable = getattr(message, "canonical_content_available", True) is False
    return {
        **metadata,
        "detail_url": reverse("inbox:message_detail", kwargs={"workspace_id": workspace_id, "message_id": message.pk}),
        "sender_name": sender["label"],
        "sender_handle": sender["handle"],
        "sender_native_id": sender["native_id"],
        "account_name": message.social_account.account_name,
        "account_handle": message.social_account.account_handle,
        "platform": message.social_account.platform,
        "message_type": message.message_type,
        "preview": "Message unavailable"
        if unavailable
        else message.body or ("Attachment" if message.attachments else "Message"),
        "timestamp": message.received_at.isoformat(),
        "status": message.status,
        "workflow_state": "",
        "canonical": None,
    }


def _project_canonical(item, metadata, workspace_id):
    latest = item["latest_message"] or {}
    preview = (
        "Message content expired"
        if latest.get("is_expired")
        else "Message unavailable"
        if latest.get("is_deleted") or latest.get("content_available") is False
        else latest.get("body") or ("Attachment" if latest.get("attachments") else "Message")
    )
    return {
        **metadata,
        "detail_url": reverse(
            "inbox:conversation_detail", kwargs={"workspace_id": workspace_id, "conversation_id": item["id"]}
        ),
        "sender_name": item["peer_name"],
        "sender_handle": item.get("peer_handle", ""),
        "sender_native_id": item.get("peer_native_id", ""),
        "account_name": item["account_name"],
        "account_handle": item["account_handle"],
        "platform": item["platform"],
        "message_type": "dm",
        "preview": preview if latest else "Messages with unavailable times",
        "timestamp": item["latest_activity_at"],
        "unread": item["read_state"]["unread"],
        "status": "",
        "workflow_state": item["workflow_state"],
        "canonical": item,
    }


def read_feed(scope, selected, *, cursor=None, limit=30):
    reader._limit(limit)
    all_sources, source_stamp = inbox_source_snapshot(scope)
    scoped = narrow_scope(
        scope,
        social_account_ids=[selected["account"]] if selected["account"] else None,
        platforms=[selected["platform"]] if selected["platform"] else None,
    )
    sources, selected_stamp = inbox_source_snapshot(scoped)
    wants_dm = selected["domain"] in {"all", "dm"}
    if (
        (selected["status"] or selected.get("view", "all") != "all")
        and wants_dm
        and any(source["source"] != "legacy" for source in sources)
    ):
        raise _invalid("Message status applies to legacy DMs. Saved canonical DMs use workflow.")
    canonical_ids = [row["id"] for row in sources if row["source"] == "canonical"] if wants_dm else []
    canonical_scope = narrow_scope(scoped, social_account_ids=canonical_ids)
    accounts, canonical_stamp = reader._snapshot(canonical_scope)
    legacy_query = _legacy_query(scoped, sources, selected)
    canonical_query = _canonical_query(canonical_scope, selected, accounts)
    legacy_meta, legacy_stamp = _legacy_metadata(legacy_query)
    canonical_meta, revision = _canonical_metadata(canonical_query)
    content_stamp = _content_stamp(scoped, sources)
    binding = digest(
        [
            scope.principal,
            scope.workspace_id,
            source_stamp,
            selected_stamp,
            canonical_stamp,
            selected,
            legacy_stamp,
            revision,
            content_stamp,
            limit,
        ]
    )
    selected_rows, next_cursor = _page(legacy_meta + canonical_meta, binding, cursor, limit)
    legacy_records = legacy_query.select_related("social_account").in_bulk(
        [row["id"] for row in selected_rows if row["source"] == "legacy"]
    )
    canonical_records = canonical_query.in_bulk([row["id"] for row in selected_rows if row["source"] == "canonical"])
    result = []
    from uuid import UUID

    for row in selected_rows:
        identifier = UUID(row["id"])
        if row["source"] == "legacy":
            if identifier not in legacy_records:
                raise reader.CanonicalReadError("stale_revision", "The inbox changed; reload.")
            result.append(_project_legacy(legacy_records[identifier], row, scope.workspace_id))
        else:
            if identifier not in canonical_records:
                raise reader.CanonicalReadError("stale_revision", "The inbox changed; reload.")
            result.append(
                _project_canonical(
                    reader._conversation(canonical_scope, accounts, canonical_records[identifier]),
                    row,
                    scope.workspace_id,
                )
            )

    def guard():
        reader._recheck(canonical_scope, canonical_stamp)
        if (
            inbox_source_snapshot(scope)[1] != source_stamp
            or inbox_source_snapshot(scoped)[1] != selected_stamp
            or _legacy_metadata(legacy_query)[1] != legacy_stamp
            or _canonical_metadata(canonical_query)[1] != revision
            or _content_stamp(scoped, sources) != content_stamp
        ):
            raise reader.CanonicalReadError("stale_revision", "The inbox changed while reading; reload.")

    guard()
    return {
        "guard": guard,
        "rows": result,
        "next_cursor": next_cursor,
        "sources": all_sources,
        "selected_sources": sources,
        "unavailable_sources": [row for row in sources if wants_dm and row["source"] == "canonical_held"],
    }


def filter_context(scope, selected, sources):
    historical = exclude_transport_projections(
        InboxMessage.objects.filter(
            workspace_id=scope.workspace_id,
            social_account__workspace_id=scope.workspace_id,
            social_account_id__in=[source["id"] for source in sources],
        )
    )
    history = {source["id"]: set() for source in sources}
    for account_id, message_type in historical.values_list("social_account_id", "message_type").distinct():
        history[str(account_id)].add(message_type)
    for account_id in (
        historical.filter(public_type_filter("mention")).values_list("social_account_id", flat=True).distinct()
    ):
        history[str(account_id)].add("mention")
    choices = []
    for source in sources:
        domains = CAPABILITIES.get(source["platform"], set()) | history[source["id"]]
        if source["source"] != "legacy":
            domains |= {"dm"}
        if not domains and source["id"] != selected["account"]:
            continue
        if selected["domain"] != "all" and selected["domain"] not in domains and source["id"] != selected["account"]:
            continue
        choices.append({**source, "domains": sorted(domains)})
    relevant = [
        source
        for source in sources
        if (not selected["account"] or source["id"] == selected["account"])
        and (not selected["platform"] or source["platform"] == selected["platform"])
    ]
    available = set().union(
        *(
            CAPABILITIES.get(source["platform"], set())
            | history[source["id"]]
            | ({"dm"} if source["source"] != "legacy" else set())
            for source in relevant
        )
    )
    path = reverse("inbox:feed", kwargs={"workspace_id": scope.workspace_id})
    tabs = []
    for domain, label in DOMAINS:
        if domain not in {"all", selected["domain"]} and domain not in available:
            continue
        params = {
            key: value
            for key, value in selected.items()
            if value and key not in {"domain", "status", "workflow", "view"}
        }
        params["domain"] = domain
        tabs.append({"value": domain, "label": label, "enabled": True, "url": path + "?" + urlencode(params)})
    platforms = {source["platform"] for source in choices} | ({selected["platform"]} if selected["platform"] else set())
    return {
        "domain_tabs": tabs,
        "account_sources": choices,
        "platform_choices": [
            (code, label) for code, label in SocialAccount._meta.get_field("platform").choices if code in platforms
        ],
    }
