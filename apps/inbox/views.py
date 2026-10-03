"""Views for the Unified Social Inbox (F-3.1)."""

import logging

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from . import services as inbox_services
from .conversations import ConversationIndex, entry_for, inbox_page
from .forms import (
    AssignForm,
    BulkActionForm,
    InternalNoteForm,
    ReplyForm,
    SavedReplyForm,
    SentimentForm,
    SLAConfigForm,
    StatusForm,
)
from .models import (
    InboxMessage,
    InboxReply,
    InboxSLAConfig,
    InternalNote,
    SavedReply,
)

logger = logging.getLogger(__name__)

MESSAGES_PER_PAGE = 50


def _detail_context(workspace, message, *, single=False, history_page=1):
    """Build the full context needed for the message detail panel."""
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()
    saved_replies = SavedReply.objects.for_workspace(workspace.id)
    team_members = WorkspaceMembership.objects.filter(
        workspace=workspace,
    ).select_related("user")
    conversation = None
    history = None
    ids = [message.id]
    members = []
    if message.message_type == InboxMessage.MessageType.DM and not single:
        scope = InboxMessage.objects.filter(workspace=workspace, social_account=message.social_account)
        members = ConversationIndex(scope).members(message.id)
        if members:
            message = scope.select_related("social_account", "assigned_to").get(id=members[-1]["id"])
            conversation = entry_for(message, members)
            ids = [row["id"] for row in members]
    replies_qs = InboxReply.objects.filter(inbox_message_id__in=ids).select_related("author")
    notes_qs = InternalNote.objects.filter(inbox_message_id__in=ids).select_related("author")
    draft_replies = list(replies_qs.exclude(status=InboxReply.Status.SENT))
    sent_qs = replies_qs.filter(status=InboxReply.Status.SENT)
    if conversation:
        # Paginate *events*, so a recent reply to an old inbound message is
        # still on the newest page. Drafts remain visible regardless of page.
        events = [("inbound", row["id"], row["received_at"]) for row in members]
        events += [
            ("reply", pk, sent or created) for pk, sent, created in sent_qs.values_list("id", "sent_at", "created_at")
        ]
        events += [("note", pk, created) for pk, created in notes_qs.values_list("id", "created_at")]
        events.sort(key=lambda event: (event[2], event[0], str(event[1])), reverse=True)
        history = Paginator(events, 100).get_page(history_page)
        by_type = {
            kind: [pk for event_kind, pk, _ in history if event_kind == kind] for kind in ("inbound", "reply", "note")
        }
        objects = {
            "inbound": InboxMessage.objects.filter(workspace=workspace, id__in=by_type["inbound"]).in_bulk(),
            "reply": sent_qs.filter(id__in=by_type["reply"]).in_bulk(),
            "note": notes_qs.filter(id__in=by_type["note"]).in_bulk(),
        }
        thread = [
            (kind, objects[kind][pk], timestamp)
            for kind, pk, timestamp in reversed(list(history))
            if pk in objects[kind]
        ]
        ids = by_type["inbound"]
    else:
        thread = sorted(
            [("reply", reply, reply.sent_at or reply.created_at) for reply in sent_qs]
            + [("note", note, note.created_at) for note in notes_qs],
            key=lambda item: item[2],
        )
    child_messages = InboxMessage.objects.filter(parent_message=message).select_related("social_account")
    return {
        "workspace": workspace,
        "message": message,
        "conversation": conversation,
        "single_message_mode": single,
        "history_page": history,
        "visible_message_ids": ids,
        "conversation_members": members,
        "thread": thread,
        "draft_replies": draft_replies,
        "child_messages": child_messages,
        "sla_config": sla_config,
        "saved_replies": saved_replies,
        "team_members": team_members,
        "reply_form": ReplyForm(),
        "note_form": InternalNoteForm(),
        "status_choices": InboxMessage.Status.choices,
    }


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    has_membership = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=workspace,
    ).exists()
    if not has_membership:
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


# --- Main Feed ---


@login_required
@require_permission("use_inbox")
def inbox_feed(request, workspace_id):
    """Main inbox feed with filtering, pagination, and split-panel layout."""
    workspace = _get_workspace(request, workspace_id)

    qs = InboxMessage.objects.for_workspace(workspace.id).select_related("social_account", "assigned_to")

    # View shortcuts
    view = request.GET.get("view", "all")
    if view == "mine":
        qs = qs.filter(assigned_to=request.user)
    elif view == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)

    # Filters
    platforms = request.GET.getlist("platform")
    if platforms:
        qs = qs.filter(social_account__platform__in=platforms)

    accounts = request.GET.getlist("account")
    if accounts:
        qs = qs.filter(social_account_id__in=accounts)

    types = request.GET.getlist("type")
    if types:
        qs = qs.filter(message_type__in=types)

    statuses = request.GET.getlist("status")
    if statuses:
        qs = qs.filter(status__in=statuses)

    assigned = request.GET.get("assigned")
    if assigned:
        qs = qs.filter(assigned_to__isnull=True) if assigned == "unassigned" else qs.filter(assigned_to_id=assigned)

    sentiments = request.GET.getlist("sentiment")
    if sentiments:
        qs = qs.filter(sentiment__in=sentiments)

    date_from = request.GET.get("date_from")
    if date_from:
        qs = qs.filter(received_at__date__gte=date_from)

    date_to = request.GET.get("date_to")
    if date_to:
        qs = qs.filter(received_at__date__lte=date_to)

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(body__icontains=q) | Q(sender_name__icontains=q) | Q(sender_handle__icontains=q))

    base = InboxMessage.objects.for_workspace(workspace.id)
    entries, page = inbox_page(base, qs, request.GET.get("page", 1), MESSAGES_PER_PAGE)
    messages = [entry.message for entry in entries]
    page_query = request.GET.copy()
    page_query.pop("page", None)

    # SLA config for countdown display
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()

    # Team members for assignment dropdown
    team_members = WorkspaceMembership.objects.filter(
        workspace=workspace,
    ).select_related("user")

    # Connected accounts for filter dropdown
    social_accounts = SocialAccount.objects.filter(
        workspace=workspace,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )

    context = {
        "workspace": workspace,
        "inbox_messages": messages,
        "inbox_entries": entries,
        "page_obj": page,
        "page_query": page_query.urlencode(),
        "sla_config": sla_config,
        "team_members": team_members,
        "social_accounts": social_accounts,
        "current_view": view,
        "active_filters": {
            "platform": platforms,
            "account": accounts,
            "type": types,
            "status": statuses,
            "assigned": assigned,
            "sentiment": sentiments,
            "date_from": date_from,
            "date_to": date_to,
            "q": q,
        },
    }

    if request.htmx:
        return render(request, "inbox/partials/_message_list.html", context)
    return render(request, "inbox/feed.html", context)


# --- Message Detail ---


@login_required
@require_permission("use_inbox")
def message_detail(request, workspace_id, message_id):
    """Message detail with thread, replies, notes, and reply composer."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account", "assigned_to"),
        id=message_id,
        workspace=workspace,
    )

    single = request.GET.get("single") == "1"
    context = _detail_context(workspace, message, single=single, history_page=request.GET.get("history_page", 1))
    # Only the displayed history is read. Read is not resolved, and newer or
    # off-page messages keep their unread state.
    InboxMessage.objects.filter(
        workspace=workspace, id__in=context["visible_message_ids"], status=InboxMessage.Status.UNREAD
    ).update(status=InboxMessage.Status.OPEN)
    visible_ids = set(context["visible_message_ids"])
    for kind, item, _timestamp in context["thread"]:
        if kind == "inbound" and item.status == InboxMessage.Status.UNREAD:
            item.status = InboxMessage.Status.OPEN
    shown = context["message"]
    if shown.id in visible_ids and shown.status == InboxMessage.Status.UNREAD:
        shown.status = InboxMessage.Status.OPEN
    if context["conversation"]:
        for member in context["conversation_members"]:
            if member["id"] in visible_ids and member["status"] == InboxMessage.Status.UNREAD:
                member["status"] = InboxMessage.Status.OPEN
        context["conversation"] = entry_for(shown, context["conversation_members"])

    if request.htmx:
        return render(request, "inbox/partials/_message_panel.html", context)
    return render(request, "inbox/message_detail.html", context)


# --- Reply ---


def _get_workspace_reply(workspace, reply_id):
    return get_object_or_404(
        InboxReply.objects.select_related("inbox_message", "inbox_message__social_account", "author"),
        id=reply_id,
        inbox_message__workspace=workspace,
    )


@login_required
@require_permission("reply_from_inbox")
@require_POST
def send_reply(request, workspace_id, message_id):
    """Send a reply via the platform API and record it."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid reply.", status=400)

    body = form.cleaned_data["body"]
    account = message.social_account

    # A reply is only recorded if the platform accepted it. Recording it
    # regardless would show the team a sent reply the customer never got.
    try:
        reply = inbox_services.send_reply(message=message, body=body, author=request.user)
    except Exception as exc:
        logger.exception("Failed to send reply for message %s (%s)", message.id, account.platform)
        response = render(
            request,
            "inbox/partials/_reply_error.html",
            {
                "platform_label": account.get_platform_display(),
                "reason": inbox_services.reply_failure_reason(exc),
            },
        )
        # htmx does not swap on a 4xx/5xx, so the failure is reported as a
        # normal swap plus a header the composer reads to keep the draft text.
        response["HX-Reply-Failed"] = "1"
        return response

    context = {"reply": reply, "workspace": workspace, "message": message}
    return render(request, "inbox/partials/_reply_item.html", context)


@login_required
@require_permission("use_inbox")
@require_POST
def save_reply_draft(request, workspace_id, message_id):
    """Save a reply as a draft without sending it."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid reply.", status=400)

    try:
        inbox_services.create_reply_draft(
            message=message,
            body=form.cleaned_data["body"],
            author=request.user,
        )
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)

    message.refresh_from_db()
    return render(
        request,
        "inbox/partials/_message_panel.html",
        _detail_context(workspace, message, single=request.POST.get("single") == "1"),
    )


@login_required
@require_permission("reply_from_inbox")
@require_POST
def send_reply_draft(request, workspace_id, reply_id):
    """Deliver an existing draft reply to the platform."""
    workspace = _get_workspace(request, workspace_id)
    reply = _get_workspace_reply(workspace, reply_id)
    message = reply.inbox_message

    failed = False
    try:
        inbox_services.send_reply_now(reply, actor=request.user)
    except inbox_services.ReplyStateError as exc:
        return HttpResponse(str(exc), status=409)
    except Exception:
        # The draft is kept (now in ``failed`` state) so the team can retry
        # or discard it; the re-rendered panel shows it with failed styling.
        logger.exception("Failed to send draft reply %s", reply.id)
        failed = True

    message.refresh_from_db()
    panel = render(
        request,
        "inbox/partials/_message_panel.html",
        _detail_context(workspace, message, single=request.POST.get("single") == "1"),
    )
    if failed:
        panel["HX-Reply-Failed"] = "1"
    return panel


@login_required
@require_permission("use_inbox")
@require_POST
def discard_reply_draft(request, workspace_id, reply_id):
    """Delete a draft (or failed) reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = _get_workspace_reply(workspace, reply_id)
    message = reply.inbox_message

    try:
        inbox_services.discard_reply_draft(reply)
    except inbox_services.ReplyStateError as exc:
        return HttpResponse(str(exc), status=409)

    message.refresh_from_db()
    return render(
        request,
        "inbox/partials/_message_panel.html",
        _detail_context(workspace, message, single=request.POST.get("single") == "1"),
    )


# --- Internal Note ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def add_note(request, workspace_id, message_id):
    """Add an internal note to a message."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = InternalNoteForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid note.", status=400)

    note = InternalNote.objects.create(
        inbox_message=message,
        author=request.user,
        body=form.cleaned_data["body"],
    )

    context = {"note": note, "workspace": workspace, "message": message}
    return render(request, "inbox/partials/_note_item.html", context)


# --- Assignment ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def assign_message(request, workspace_id, message_id):
    """Assign a message to a team member."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = AssignForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid assignment.", status=400)

    assigned_to_id = form.cleaned_data.get("assigned_to")
    if assigned_to_id:
        # Verify the user is a workspace member
        membership = (
            WorkspaceMembership.objects.filter(workspace=workspace, user_id=assigned_to_id)
            .select_related("user")
            .first()
        )
        if not membership:
            return HttpResponse("User is not a workspace member.", status=400)
        message.assigned_to = membership.user
    else:
        message.assigned_to = None

    message.save(update_fields=["assigned_to"])

    # Notify the assignee
    if message.assigned_to and message.assigned_to != request.user:
        notify(
            user=message.assigned_to,
            event_type=EventType.NEW_INBOX_MESSAGE,
            title=f"You were assigned a {message.get_message_type_display()}",
            body=f"From {message.sender_name}: {message.body[:100]}",
            data={
                "message_id": str(message.id),
                "workspace_id": str(workspace.id),
            },
        )

    context = _detail_context(workspace, message, single=request.POST.get("single") == "1")
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Status ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def change_status(request, workspace_id, message_id):
    """Change message status."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = StatusForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid status.", status=400)

    message.status = form.cleaned_data["status"]
    message.save(update_fields=["status"])

    context = _detail_context(workspace, message, single=request.POST.get("single") == "1")
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Sentiment Override ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def change_sentiment(request, workspace_id, message_id):
    """Override sentiment manually."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = SentimentForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid sentiment.", status=400)

    message.sentiment = form.cleaned_data["sentiment"]
    message.sentiment_source = InboxMessage.SentimentSource.MANUAL
    message.save(update_fields=["sentiment", "sentiment_source"])

    context = {"message": message, "workspace": workspace}
    return render(request, "inbox/partials/_sentiment_badge.html", context)


# --- Bulk Actions ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def bulk_action(request, workspace_id):
    """Perform bulk actions on multiple messages."""
    workspace = _get_workspace(request, workspace_id)

    form = BulkActionForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid bulk action.", status=400)

    message_ids = form.cleaned_data["message_ids"]
    action = form.cleaned_data["action"]
    value = form.cleaned_data.get("value", "")

    qs = InboxMessage.objects.filter(id__in=message_ids, workspace=workspace)

    if action == "mark_read":
        qs.filter(status=InboxMessage.Status.UNREAD).update(status=InboxMessage.Status.OPEN)
    elif action == "resolve":
        qs.exclude(status=InboxMessage.Status.ARCHIVED).update(status=InboxMessage.Status.RESOLVED)
    elif action == "archive":
        qs.update(status=InboxMessage.Status.ARCHIVED)
    elif action == "assign" and value:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user_id=value).first()
        if membership:
            qs.update(assigned_to=membership.user)

    # Re-fetch and return updated list
    base = InboxMessage.objects.for_workspace(workspace.id)
    entries, page = inbox_page(base, base, page_size=MESSAGES_PER_PAGE)
    context = {
        "workspace": workspace,
        "inbox_messages": [entry.message for entry in entries],
        "inbox_entries": entries,
        "page_obj": page,
    }
    return render(request, "inbox/partials/_message_list.html", context)


# --- Saved Replies ---


@login_required
@require_permission("manage_workspace_settings")
def saved_replies_list(request, workspace_id):
    """List saved replies for a workspace."""
    workspace = _get_workspace(request, workspace_id)
    replies = SavedReply.objects.for_workspace(workspace.id)

    context = {"workspace": workspace, "saved_replies": replies}
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
def saved_reply_create(request, workspace_id):
    """Create a new saved reply."""
    workspace = _get_workspace(request, workspace_id)

    if request.method == "POST":
        form = SavedReplyForm(request.POST)
        if form.is_valid():
            reply = form.save(commit=False)
            reply.workspace = workspace
            reply.created_by = request.user
            reply.save()
            return redirect("inbox:saved_replies", workspace_id=workspace.id)
    else:
        form = SavedReplyForm()

    context = {"workspace": workspace, "form": form}

    if request.htmx:
        return render(request, "inbox/partials/_saved_reply_form.html", context)
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
def saved_reply_edit(request, workspace_id, reply_id):
    """Edit an existing saved reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = get_object_or_404(SavedReply, id=reply_id, workspace=workspace)

    if request.method == "POST":
        form = SavedReplyForm(request.POST, instance=reply)
        if form.is_valid():
            form.save()
            return redirect("inbox:saved_replies", workspace_id=workspace.id)
    else:
        form = SavedReplyForm(instance=reply)

    context = {"workspace": workspace, "form": form, "saved_reply": reply}

    if request.htmx:
        return render(request, "inbox/partials/_saved_reply_form.html", context)
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def saved_reply_delete(request, workspace_id, reply_id):
    """Delete a saved reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = get_object_or_404(SavedReply, id=reply_id, workspace=workspace)
    reply.delete()
    return redirect("inbox:saved_replies", workspace_id=workspace.id)


# --- SLA Config ---


@login_required
@require_permission("manage_workspace_settings")
def sla_config(request, workspace_id):
    """Configure SLA settings for inbox."""
    workspace = _get_workspace(request, workspace_id)
    config, _created = InboxSLAConfig.objects.get_or_create(
        workspace=workspace,
        defaults={"target_response_minutes": 120, "is_active": False},
    )

    if request.method == "POST":
        form = SLAConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            return redirect("inbox:feed", workspace_id=workspace.id)
    else:
        form = SLAConfigForm(instance=config)

    context = {"workspace": workspace, "form": form, "config": config}
    return render(request, "inbox/sla_config.html", context)
