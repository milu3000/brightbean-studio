"""Views for the Unified Social Inbox (F-3.1)."""

import logging
from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages as flash_messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from . import presentation
from . import services as inbox_services
from .dm_send_gate import DMSendGateError, dm_send_status, session_send_authorization
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
from .reply_safety import is_unresolved_reply

logger = logging.getLogger(__name__)

MESSAGES_PER_PAGE = 50
DRAFT_CREATION_HOLDS = {
    "existing_draft",
    "target_answered",
    "outcome_unknown",
    "existing_operation",
    "legacy_outcome_unverified",
    "invalid_follow_up",
    "follow_up_exists",
}


def _detail_context(workspace, message, request=None, *, history_before=None):
    """Build the full context needed for the message detail panel."""
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()
    saved_replies = SavedReply.objects.for_workspace(workspace.id)
    team_members = WorkspaceMembership.objects.filter(
        workspace=workspace,
    ).select_related("user")
    conversation_view = presentation.enabled() and message.message_type == InboxMessage.MessageType.DM
    stored_messages = presentation.stored_thread_messages(message)
    if not conversation_view:
        stored_messages = stored_messages.filter(pk=message.pk)
    reply_query = InboxReply.objects.filter(inbox_message__in=stored_messages)
    if conversation_view:
        reply_query = reply_query.exclude(status=InboxReply.Status.SENT)
    replies = list(
        reply_query.select_related("author", "inbox_message", "inbox_message__social_account", "follow_up_of")
    )
    notes = list(message.internal_notes.select_related("author")) if not conversation_view else []
    reply_target = stored_messages.select_related("social_account").order_by("-received_at", "-pk").first() or message
    follow_up_id = request.GET.get("follow_up_reply_id", "") if request and request.method == "GET" else ""
    if follow_up_id:
        reply_target = message
    composer = _composer_context(reply_target, follow_up_id)
    membership = getattr(request, "workspace_membership", None)
    permissions = membership.effective_permissions if membership else {}
    can_review_delivery = all(
        permissions.get(key, False) for key in ("use_inbox", "manage_workspace_settings", "reply_from_inbox")
    )
    for reply in replies:
        if reply.status != InboxReply.Status.SENT:
            reply.send_availability = _send_availability(reply.inbox_message, reply=reply)
            reply.display_error = _display_send_reason(reply.send_error)
            reply.is_unresolved = is_unresolved_reply(reply)
            reply.can_edit = (
                reply.status in {InboxReply.Status.DRAFT, InboxReply.Status.FAILED}
                and not reply.is_unresolved
                and not (reply.is_follow_up and not reply.follow_up_of_id)
                and not reply.dm_send_attempts.exists()
                and not hasattr(reply, "send_operation")
            )
    # Sent replies sit in the chronological thread; drafts (and failed
    # sends awaiting a retry) are pending work, surfaced by the composer.
    sent_replies = [r for r in replies if r.status == InboxReply.Status.SENT]
    draft_replies = [r for r in replies if r.status != InboxReply.Status.SENT]
    thread = sorted(
        [("reply", r, r.sent_at or r.created_at) for r in sent_replies] + [("note", n, n.created_at) for n in notes],
        key=lambda x: x[2],
    )
    child_messages = InboxMessage.objects.filter(
        parent_message=message,
        workspace=workspace,
        social_account_id=message.social_account_id,
        social_account__workspace=workspace,
    ).select_related("social_account")
    history = (
        presentation.timeline_page(
            stored_messages,
            1 if history_before else request.GET.get("history_page", 1) if request else 1,
            before=history_before,
        )
        if conversation_view
        else None
    )
    if history is not None:
        thread = history.object_list
    return {
        "workspace": workspace,
        "message": message,
        **composer,
        "conversation_view": conversation_view,
        "has_native_thread": bool(presentation.thread_id(message)),
        "native_view_scope": (
            presentation.native_view_scope(message, request.user.pk)
            if conversation_view and request and permissions.get("use_inbox") is True
            else ""
        ),
        "stored_message_count": stored_messages.count(),
        "history_page": history,
        "timeline_events": presentation.timeline_events(thread) if history is not None else [],
        "history_page_key": request.GET.get("history_before", "") if request else "",
        "history_older_url": (
            reverse("inbox:message_detail", kwargs={"workspace_id": workspace.pk, "message_id": message.pk})
            + "?"
            + urlencode({"history_before": presentation.history_cursor(message, thread[0])})
            if history is not None and history.has_next() and thread
            else ""
        ),
        "selected_outside_history": bool(history)
        and not any(kind == "incoming" and item.pk == message.pk for kind, item, _ in thread),
        "thread": thread,
        "draft_replies": draft_replies,
        "can_review_delivery": can_review_delivery,
        "child_messages": child_messages,
        "sla_config": sla_config,
        "saved_replies": saved_replies,
        "team_members": team_members,
        "reply_form": ReplyForm(),
        "note_form": InternalNoteForm(),
        "status_choices": InboxMessage.Status.choices,
    }


def _send_availability(message, *, reply=None, follow_up_of=None):
    result = inbox_services.reply_send_availability(message, reply=reply, follow_up_of=follow_up_of)
    result["reason"] = _display_send_reason(result["reason"], result["code"])
    return result


def _follow_up_parent(message, identifier):
    """Only a confirmed receipt on this exact original incoming is a parent."""
    parent_id = _valid_uuid(identifier)
    parent = (
        InboxReply.objects.select_related("inbox_message", "inbox_message__social_account")
        .filter(
            pk=parent_id,
            inbox_message_id=message.pk,
            inbox_message__workspace_id=message.workspace_id,
            inbox_message__social_account_id=message.social_account_id,
            inbox_message__social_account__workspace_id=message.workspace_id,
            status=InboxReply.Status.SENT,
        )
        .first()
        if parent_id and message.message_type == InboxMessage.MessageType.DM
        else None
    )
    if parent is None:
        raise DMSendGateError(
            "invalid_follow_up",
            "The selected sent reply is no longer available for an additional reply. Select a confirmed sent reply again.",
        )
    return parent


def _composer_context(target, follow_up_id=""):
    parent = None
    try:
        if follow_up_id:
            parent = _follow_up_parent(target, follow_up_id)
        availability = _send_availability(target, follow_up_of=parent)
        if follow_up_id and availability["allowed"] and availability.get("existing_reply_id"):
            availability.update(
                allowed=False,
                code="existing_draft",
                reason="An additional reply draft already exists. Review it in Pending replies.",
            )
    except DMSendGateError as exc:
        availability = {"allowed": False, "code": exc.code, "reason": str(exc), "existing_reply_id": None}
    return {
        "reply_target": target,
        "follow_up_reply_id": follow_up_id,
        "follow_up_parent": parent,
        "send_availability": availability,
        "can_save_draft": availability["allowed"] if follow_up_id else availability["code"] not in DRAFT_CREATION_HOLDS,
    }


def _display_send_reason(reason, code=""):
    if code in {"conversation_owned", "ownership_required", "existing_operation"}:
        return "A reply is managed by the conversation's assigned responder. Sending here is unavailable."
    if "V2" in reason or "durable DM attempts" in reason:
        return "This reply is already being handled and cannot be changed here. Review its current status."
    return reason


def _panel_message(request, target):
    """Keep the visible selection only if it belongs to this same stored thread."""
    try:
        selected_id = UUID(request.POST.get("panel_message_id", ""))
    except (ValueError, TypeError, AttributeError):
        return target
    return (
        presentation.stored_thread_messages(target)
        .select_related("social_account", "assigned_to")
        .filter(pk=selected_id)
        .first()
        or target
    )


def _render_panel(request, workspace, target, **extra):
    message = _panel_message(request, target)
    context = _detail_context(workspace, message, request)
    context.update(extra)
    if extra.get("composer_body") or extra.get("follow_up_reply_id"):
        # A failure must not move the user's unsaved text to a newer incoming
        # message that arrived while the request was in flight.
        context.update(_composer_context(target, extra.get("follow_up_reply_id", "")))
    response = render(request, "inbox/partials/_message_panel.html", context)
    response["HX-Retarget"] = "#inbox-detail-panel"
    response["HX-Reswap"] = "innerHTML"
    response["HX-Trigger"] = "inbox:refresh"
    return response


def _list_query(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


def _valid_uuid(value):
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        return None


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

    qs = InboxMessage.objects.for_workspace(workspace.id).filter(social_account__workspace=workspace)

    # View shortcuts
    view = request.GET.get("view", "all")
    if view == "mine":
        qs = qs.filter(assigned_to=request.user)
    elif view == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)

    # Filters
    platforms = [value for value in request.GET.getlist("platform") if value]
    if platforms:
        qs = qs.filter(social_account__platform__in=platforms)

    accounts = [value for value in request.GET.getlist("account") if value]
    if accounts:
        account_ids = [_valid_uuid(value) for value in accounts]
        qs = qs.filter(social_account_id__in=account_ids) if all(account_ids) else qs.none()

    types = [value for value in request.GET.getlist("type") if value]
    if types:
        qs = qs.filter(message_type__in=types)

    statuses = [value for value in request.GET.getlist("status") if value]
    if statuses:
        qs = qs.filter(status__in=statuses)

    assigned = request.GET.get("assigned")
    if assigned:
        if assigned == "unassigned":
            qs = qs.filter(assigned_to__isnull=True)
        else:
            assigned_id = _valid_uuid(assigned)
            qs = qs.filter(assigned_to_id=assigned_id) if assigned_id else qs.none()

    sentiments = [value for value in request.GET.getlist("sentiment") if value]
    if sentiments:
        qs = qs.filter(sentiment__in=sentiments)

    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    for value, lookup in ((date_from, "received_at__date__gte"), (date_to, "received_at__date__lte")):
        if value:
            try:
                parsed = parse_date(value)
            except ValueError:
                parsed = None
            qs = qs.filter(**{lookup: parsed}) if parsed else qs.none()

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(body__icontains=q) | Q(sender_name__icontains=q) | Q(sender_handle__icontains=q))

    page = presentation.inbox_page(qs, request.GET.get("page", 1), per_page=MESSAGES_PER_PAGE)

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
        "inbox_messages": [row.message for row in page],
        "inbox_rows": page.object_list,
        "inbox_page": page,
        "list_query": _list_query(request),
        "conversation_presentation": presentation.enabled(),
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
        social_account__workspace=workspace,
    )

    history_before = None
    if "history_before" in request.GET:
        try:
            history_before = presentation.parse_history_cursor(message, request.GET["history_before"])
        except ValueError:
            return HttpResponse("This history position is invalid or expired. Reopen the conversation.", status=400)

    # Mark as read → open
    marked_read = False
    if message.status == InboxMessage.Status.UNREAD:
        marked_read = bool(
            InboxMessage.objects.filter(pk=message.pk, status=InboxMessage.Status.UNREAD).update(
                status=InboxMessage.Status.OPEN
            )
        )
        message.refresh_from_db(fields=["status"])

    context = _detail_context(workspace, message, request, history_before=history_before)

    if request.htmx:
        if request.headers.get("HX-Target") == "inbox-thread" and context["conversation_view"]:
            return render(request, "inbox/partials/_conversation_timeline.html", context)
        response = render(request, "inbox/partials/_message_panel.html", context)
        if marked_read:
            response["HX-Trigger"] = "inbox:refresh"
        return response
    return render(request, "inbox/message_detail.html", context)


@never_cache
@login_required
@require_permission("use_inbox")
@require_POST
def native_thread_refresh(request, workspace_id, message_id):
    """Read a temporary page after a scoped, CSRF-protected conversation interaction."""
    from .native_thread_reads import NativeThreadReadError, read_native_thread, session_read_authorization

    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account"),
        id=message_id,
        workspace=workspace,
        social_account__workspace=workspace,
    )
    try:
        continuation = request.POST.get("continuation")
        if len(request.POST.getlist("continuation")) > 1:
            raise NativeThreadReadError("invalid_continuation", "This history position is invalid or expired.")
        options = {"continuation": continuation} if continuation is not None else {}
        result = read_native_thread(
            message, authorization=session_read_authorization(request.user), limit=50, **options
        )
    except NativeThreadReadError as exc:
        # Never return raw provider errors or re-render the composer/timeline.
        result = {"status": "unavailable", "reason_code": exc.code, "anchor_message_id": str(message.pk)}
        status = {
            "authorization_required": 403,
            "authorization_revoked": 403,
            "invalid_limit": 400,
            "invalid_continuation": 400,
            "stale_continuation": 409,
        }.get(exc.code, 409)
        return JsonResponse(result, status=status)
    except Exception:
        # Provider exceptions can contain credentials or message bodies. Keep
        # this boundary content-free, including logs and browser error output.
        result = {"status": "unavailable", "reason_code": "read_failed", "anchor_message_id": str(message.pk)}
        return JsonResponse(result, status=502)
    return JsonResponse(result)


# --- Reply ---


def _get_workspace_reply(workspace, reply_id):
    return get_object_or_404(
        InboxReply.objects.select_related("inbox_message", "inbox_message__social_account", "author"),
        id=reply_id,
        inbox_message__workspace=workspace,
        inbox_message__social_account__workspace=workspace,
    )


def _aware_input(value):
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else None
    try:
        parsed = parse_datetime(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed is not None and timezone.is_aware(parsed) else None


def _review_generation(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _lookup_display(result):
    """Render only bounded receipt references, never external message bodies."""
    result = result if isinstance(result, dict) else {}
    candidates = []
    raw_candidates = result.get("candidates", [])
    if isinstance(raw_candidates, list):
        for candidate in raw_candidates[:5]:
            if not isinstance(candidate, dict):
                continue
            receipt_id = candidate.get("platform_reply_id")
            if not isinstance(receipt_id, str) or not receipt_id or len(receipt_id) > 255:
                continue
            timestamp = _aware_input(candidate.get("sent_at"))
            candidates.append({"platform_reply_id": receipt_id, "sent_at": timestamp.isoformat() if timestamp else ""})
    return {
        "candidates": candidates,
        "more_available": result.get("more_available") is True,
        "checked_at": _aware_input(result.get("checked_at")),
    }


@login_required
@require_permission("use_inbox")
@require_permission("manage_workspace_settings")
@require_permission("reply_from_inbox")
@require_http_methods(["GET", "POST"])
def review_reply_outcome(request, workspace_id, reply_id):
    """Record a manager's verified finding without sending or retrying a reply."""
    from .reply_reconciliation import reconcile_reply_outcome, reconciliation_availability

    workspace = _get_workspace(request, workspace_id)
    reply = _get_workspace_reply(workspace, reply_id)
    availability = reconciliation_availability(reply, actor=request.user)
    values = {
        "expected_updated_at": reply.updated_at.isoformat(),
        "expected_send_generation": str(reply.send_generation),
        "outcome": "",
        "platform_reply_id": "",
        "sent_at": "",
        "confirmed": False,
    }
    errors = []
    status = 200
    lookup_result = None
    if request.method == "POST" and request.POST.get("action") == "lookup":
        values["expected_updated_at"] = request.POST.get("expected_updated_at", "")
        values["expected_send_generation"] = request.POST.get("expected_send_generation", "")
        expected = _aware_input(values["expected_updated_at"])
        generation = _review_generation(values["expected_send_generation"])
        if expected is None or generation is None:
            errors.append("The review version is missing or invalid. Reload this page before looking up the receipt.")
            status = 400
        elif generation != reply.send_generation:
            errors.append("This reply has a newer sending attempt. Reload and review its current state.")
            status = 409
        else:
            from .reply_lookup import lookup_reply_receipts

            try:
                lookup_result = _lookup_display(
                    lookup_reply_receipts(
                        reply=reply,
                        actor=request.user,
                        expected_updated_at=expected,
                        expected_send_generation=generation,
                    )
                )
            except inbox_services.ReplyStateError as exc:
                errors.append(_display_send_reason(str(exc), getattr(exc, "code", "")))
                status = 403 if getattr(exc, "code", "").endswith("denied") else 409
            except Exception:
                logger.warning("Platform receipt lookup could not complete for reply %s", reply.pk)
                lookup_result = _lookup_display({})
    elif request.method == "POST":
        values = {key: request.POST.get(key, "") for key in values}
        values["confirmed"] = request.POST.get("confirmed") == "yes"
        expected = _aware_input(values["expected_updated_at"])
        generation = _review_generation(values["expected_send_generation"])
        sent_at = _aware_input(values["sent_at"]) if values["sent_at"] else None
        if values["outcome"] not in {"sent", "not_sent"}:
            errors.append("Choose the outcome you personally verified on the original platform.")
        if not values["confirmed"]:
            errors.append("Confirm that you personally checked the original platform and verified this outcome.")
        if expected is None or generation is None:
            errors.append("The review version is missing or invalid. Reload this page before reviewing again.")
        if values["outcome"] == "sent":
            if not values["platform_reply_id"].strip() or len(values["platform_reply_id"]) > 255:
                errors.append("Enter the platform's message ID for the reply you verified as sent.")
            if sent_at is None:
                errors.append("Enter the verified sent time with a timezone, such as 2026-10-06T15:30:00+08:00.")
        if errors:
            status = 400
        elif generation != reply.send_generation:
            errors.append("This reply has a newer sending attempt. Reload and review its current state.")
            status = 409
        else:
            try:
                reconcile_reply_outcome(
                    reply=reply,
                    actor=request.user,
                    expected_updated_at=expected,
                    expected_send_generation=generation,
                    outcome=values["outcome"],
                    platform_reply_id=values["platform_reply_id"].strip(),
                    sent_at=sent_at,
                    confirmed=values["confirmed"],
                )
            except inbox_services.ReplyStateError as exc:
                errors.append(_display_send_reason(str(exc), getattr(exc, "code", "")))
                status = 403 if getattr(exc, "code", "") == "reconciliation_denied" else 409
                reply.refresh_from_db()
                availability = reconciliation_availability(reply, actor=request.user)
            else:
                flash_messages.success(request, "Your manual delivery review was recorded.")
                return redirect("inbox:message_detail", workspace_id=workspace.pk, message_id=reply.inbox_message_id)
    return render(
        request,
        "inbox/review_reply_outcome.html",
        {
            "workspace": workspace,
            "reply": reply,
            "message": reply.inbox_message,
            "availability": availability,
            "values": values,
            "review_errors": errors,
            "lookup_result": lookup_result,
            "can_lookup": availability["allowed"],
        },
        status=status,
    )


@login_required
@require_permission("reply_from_inbox")
@require_POST
def send_reply(request, workspace_id, message_id):
    """Send a reply via the platform API and record it."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace, social_account__workspace=workspace)

    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid reply.", status=400)

    body = form.cleaned_data["body"]
    follow_up_id = request.POST.get("follow_up_reply_id", "")
    account = message.social_account

    # Only confirmed receipts appear in the sent timeline. Draft, failed and
    # uncertain outcomes remain visible beside the composer.
    try:
        parent = _follow_up_parent(message, follow_up_id) if follow_up_id else None
        inbox_services.send_reply(
            message=message,
            body=body,
            author=request.user,
            authorization=session_send_authorization(request.user),
            follow_up_of=parent,
        )
    except Exception as exc:
        logger.exception("Failed to send reply for message %s (%s)", message.id, account.platform)
        # A durable unknown or failed receipt belongs beside the composer, never
        # in the sent timeline. Keep unsaved text when no matching draft exists.
        saved = False
        if not follow_up_id or _valid_uuid(follow_up_id):
            saved = (
                message.replies.filter(body=body, follow_up_of_id=_valid_uuid(follow_up_id))
                .exclude(status=InboxReply.Status.SENT)
                .exists()
            )
        response = _render_panel(
            request,
            workspace,
            message,
            dm_gate_reason=_display_send_reason(
                str(exc)
                if isinstance(exc, inbox_services.ReplyStateError)
                else inbox_services.reply_failure_reason(exc),
                getattr(exc, "code", ""),
            ),
            reply_error_title="Sending held" if isinstance(exc, DMSendGateError) else "Reply not sent",
            composer_body="" if saved else body,
            follow_up_reply_id=follow_up_id,
        )
        response["HX-Reply-Failed"] = "1"
        return response

    return _render_panel(request, workspace, message)


@login_required
@require_permission("use_inbox")
@require_POST
def save_reply_draft(request, workspace_id, message_id):
    """Save a reply as a draft without sending it."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace, social_account__workspace=workspace)

    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid reply.", status=400)

    follow_up_id = request.POST.get("follow_up_reply_id", "")
    try:
        parent = _follow_up_parent(message, follow_up_id) if follow_up_id else None
        inbox_services.create_reply_draft(
            message=message,
            body=form.cleaned_data["body"],
            author=request.user,
            follow_up_of=parent,
        )
    except ValueError as exc:
        response = _render_panel(
            request,
            workspace,
            message,
            dm_gate_reason=str(exc),
            composer_body=form.cleaned_data["body"],
            follow_up_reply_id=follow_up_id,
        )
        response["HX-Reply-Failed"] = "1"
        return response

    message.refresh_from_db()
    return _render_panel(request, workspace, message)


@login_required
@require_permission("use_inbox")
@require_POST
def update_reply_draft(request, workspace_id, reply_id):
    """Edit the existing draft body without changing its original message."""
    workspace = _get_workspace(request, workspace_id)
    reply = _get_workspace_reply(workspace, reply_id)
    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid reply.", status=400)
    try:
        inbox_services.update_reply_draft(reply, body=form.cleaned_data["body"])
    except inbox_services.ReplyStateError as exc:
        response = _render_panel(
            request,
            workspace,
            reply.inbox_message,
            dm_gate_reason=_display_send_reason(str(exc)),
            unsaved_draft_body=form.cleaned_data["body"],
        )
        response["HX-Reply-Failed"] = "1"
        return response
    return _render_panel(request, workspace, reply.inbox_message)


@login_required
@require_permission("reply_from_inbox")
@require_POST
def send_reply_draft(request, workspace_id, reply_id):
    """Deliver an existing draft reply to the platform."""
    workspace = _get_workspace(request, workspace_id)
    reply = _get_workspace_reply(workspace, reply_id)
    message = reply.inbox_message

    failed = False
    gate_reason = ""
    try:
        inbox_services.send_reply_now(reply, actor=request.user, authorization=session_send_authorization(request.user))
    except DMSendGateError as exc:
        # Render committed unknown/hold state immediately. HTMX does not swap
        # a 409, which would leave stale Retry/Discard controls on the screen.
        failed = True
        gate_reason = _display_send_reason(str(exc), exc.code)
    except inbox_services.ReplyStateError as exc:
        failed = True
        gate_reason = _display_send_reason(str(exc))
    except Exception:
        # The draft is kept (now in ``failed`` state) so the team can retry
        # or discard it; the re-rendered panel shows it with failed styling.
        logger.exception("Failed to send draft reply %s", reply.id)
        failed = True

    message.refresh_from_db()
    panel = _render_panel(request, workspace, message, dm_gate_reason=gate_reason)
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
    return _render_panel(request, workspace, message)


# --- Internal Note ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def add_note(request, workspace_id, message_id):
    """Add an internal note to a message."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace, social_account__workspace=workspace)

    form = InternalNoteForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid note.", status=400)

    InternalNote.objects.create(
        inbox_message=message,
        author=request.user,
        body=form.cleaned_data["body"],
    )

    return _render_panel(request, workspace, message)


# --- Assignment ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def assign_message(request, workspace_id, message_id):
    """Assign a message to a team member."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace, social_account__workspace=workspace)

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
            title=f"You were assigned a {message.type_display}",
            body=f"From {message.sender_name}: {message.body[:100]}",
            data={
                "message_id": str(message.id),
                "workspace_id": str(workspace.id),
            },
        )

    return _render_panel(request, workspace, message)


# --- Status ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def change_status(request, workspace_id, message_id):
    """Change message status."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace, social_account__workspace=workspace)

    form = StatusForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid status.", status=400)

    message.status = form.cleaned_data["status"]
    message.save(update_fields=["status"])

    return _render_panel(request, workspace, message)


# --- Sentiment Override ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def change_sentiment(request, workspace_id, message_id):
    """Override sentiment manually."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace, social_account__workspace=workspace)

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

    return inbox_feed(request, workspace_id)


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


@login_required
@require_permission("use_inbox")
@require_GET
def dm_send_gate_status(request, workspace_id, account_id):
    """Existing session authorization, facts only; no control mutation."""
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, pk=account_id, workspace=workspace)
    try:
        state = dm_send_status(account)
    except DMSendGateError as exc:
        return JsonResponse({"detail": str(exc)}, status=409)
    return JsonResponse(state)
