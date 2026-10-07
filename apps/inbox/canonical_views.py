"""Session pages for the persisted DM reader; the rollout remains disabled."""

from urllib.parse import urlencode
from uuid import UUID

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount

from .dm_send_gate import DMSendGateError, session_send_authorization
from .forms import ReplyForm
from .models import InboxConversation, InboxReply


def enabled():
    return getattr(settings, "INBOX_CANONICAL_READ_ENABLED", False) is True


def _reader():
    from . import canonical_reads

    return canonical_reads


def _error(exc):
    code = getattr(exc, "code", "not_found_or_denied")
    if code == "not_found_or_denied":
        return HttpResponse("Conversation unavailable.", status=404)
    if code in {"invalid_filter", "invalid_limit", "invalid_search"}:
        return HttpResponse("Invalid inbox filter.", status=422)
    return HttpResponse("Conversation changed. Reload the inbox.", status=409)


def _detail_url(workspace, identifier, cursor=None):
    url = reverse("inbox:conversation_detail", kwargs={"workspace_id": workspace.pk, "conversation_id": identifier})
    return url + "?" + urlencode({"cursor": cursor, "fragment": "history"}) if cursor else url


def _timeline_context(workspace, result, request):
    from .message_details import retained_capabilities

    all_items = result["messages"] + result["undated_messages"]
    retained = retained_capabilities(
        _reader().session_read_scope(request.user, workspace.pk),
        result["conversation"]["id"],
        [row["id"] for row in all_items],
    )
    for row in all_items:
        row["retained_available"] = row["id"] in retained
    conversation = result["conversation"]
    reader = _reader()
    scope = reader.session_read_scope(request.user, workspace.pk)
    accounts, stamp = reader._snapshot(scope)
    current = InboxConversation.objects.filter(
        reader._scope_filter(scope, accounts), pk=conversation["id"], revision=conversation["revision"]
    ).first()
    if current is None:
        raise reader.CanonicalReadError("stale_revision", "Conversation changed.")
    from .canonical_access import digest

    view_scope = digest([stamp, reader._identity(current)])
    reader._recheck(scope, stamp)
    permissions = getattr(request, "workspace_membership", None)
    permissions = permissions.effective_permissions if permissions else {}
    return {
        "workspace": workspace,
        "canonical_conversation": conversation,
        "canonical_messages": result["messages"],
        "canonical_view_scope": view_scope,
        "canonical_latest_url": _detail_url(workspace, conversation["id"]) + "?fragment=history",
        "composer_observation_token": result.get("composer_observation_token") or "",
        "can_mark_done": bool(
            conversation["workflow_tracking_available"]
            and conversation["conversation_type"] == "direct"
            and conversation["workflow_state"] in {"needs_action", "waiting"}
            and permissions.get("reply_from_inbox") is True
            and not conversation.get("archived")
        ),
        "canonical_undated": result["undated_messages"],
        "canonical_older_url": _detail_url(workspace, conversation["id"], result["next_cursor"])
        if result["next_cursor"]
        else "",
        "canonical_undated_url": _detail_url(workspace, conversation["id"], result["undated_next_cursor"])
        if result["undated_next_cursor"]
        else "",
        "read_ack_token": result.get("read_ack_token") or "",
        "sync_failed": result["coverage"]["status"] == "failed",
    }


def _reply_content(reply):
    from .receipt_compaction import reply_display_content

    content = reply_display_content(reply)
    reply.display_body = content["body"]
    reply.content_available = content["available"]
    reply.content_expired = content["is_expired"]
    return reply


def _panel_context(request, workspace, result, *, observation_token=None):
    from .conversation_composer import composer_context, session_read_authorization

    reader = _reader()
    scope = reader.session_read_scope(request.user, workspace.pk)
    _, guard = reader._snapshot(scope)
    context = _timeline_context(workspace, result, request)
    if observation_token is None and request.method != "GET":
        observation_token = request.POST.get("composer_observation_token", "")
    if observation_token is not None:
        context["composer_observation_token"] = observation_token
    item = result["conversation"]
    target = (
        InboxConversation.objects.select_related("social_account")
        .filter(
            pk=item["id"],
            workspace=workspace,
            social_account_id=item["social_account_id"],
            social_account__workspace=workspace,
            platform=item["platform"],
            revision=item["revision"],
        )
        .first()
    )
    if target is None:
        raise _reader().CanonicalReadError("stale_revision", "Conversation changed.")
    adopted = request.GET.get("adopt_reply_id") if request.method == "GET" else None
    state = composer_context(
        target,
        authorization=session_read_authorization(request.user),
        send_authorization=session_send_authorization(request.user),
        automated=False,
        adopt_reply_id=adopted,
        observation_token=context["composer_observation_token"],
    )
    active = state["active_reply"] or state.get("adopt_reply")
    if active is not None:
        _reply_content(active)
    permissions = getattr(request, "workspace_membership", None)
    permissions = permissions.effective_permissions if permissions else {}
    availability = dict(state["send_availability"])
    if availability.get("code") in {"stale_observation", "scope_changed"}:
        availability["reason"] = "Select Latest before sending."
    can_save = state.get("can_save_draft", False)
    if state["requires_legacy_adoption"] and not active:
        availability = {"allowed": False, "code": "adoption_required", "reason": "Choose a saved draft to continue."}
        can_save = False
    if active is not None and (not active.content_available or active.status != "draft"):
        availability = {
            "allowed": False,
            "code": "receipt_review",
            "reason": "Review the pending reply before sending another message.",
        }
        can_save = False
    if permissions.get("reply_from_inbox") is not True:
        availability = {"allowed": False, "reason": "You do not have permission to send replies."}
    if item.get("archived"):
        availability = {"allowed": False, "reason": "This account is disconnected."}
        can_save = False
    from .pending_history import page as pending_page

    pending_context = pending_page(request, workspace, target, active=active)
    from .canonical_access import digest

    context.update(
        composer_refresh_key=digest([availability, state["composer_revision"], state.get("ownership")]),
        needs_latest=availability.get("code") in {"stale_observation", "scope_changed"},
        canonical_target=target,
        conversation_composer=state,
        composer_active_reply=active,
        composer_body=active.display_body if active and active.status == "draft" else "",
        can_save_draft=can_save,
        send_availability=availability,
        pinned_receipt=active if active and active.status != "draft" else None,
        **pending_context,
        can_review_delivery=all(
            permissions.get(key) for key in ("use_inbox", "manage_workspace_settings", "reply_from_inbox")
        ),
        can_mark_done=bool(
            item["workflow_tracking_available"]
            and item["conversation_type"] == "direct"
            and item["workflow_state"] in {"needs_action", "waiting"}
            and permissions.get("reply_from_inbox") is True
            and not item.get("archived")
        ),
    )
    reader._recheck(scope, guard)
    if not InboxConversation.objects.filter(pk=target.pk, revision=item["revision"]).exists():
        raise reader.CanonicalReadError("stale_revision", "Conversation changed.")
    return context


def feed(request, workspace):
    reader = _reader()
    if any(value.strip() for value in request.GET.getlist("sentiment")):
        return HttpResponse(
            "Sentiment filtering has been retired. Remove the sentiment filter to continue.", status=400
        )
    if (
        any(request.GET.get(key) for key in ("status", "assigned", "date_from", "date_to"))
        or request.GET.get("view", "all") not in {"", "all"}
        or any(value not in {"", "dm"} for value in request.GET.getlist("type"))
    ):
        return HttpResponse(
            "This saved filter is unavailable here. Choose filters from the conversation inbox.", status=422
        )
    if any(len(request.GET.getlist(key)) > 1 for key in ("q", "account", "platform", "workflow", "cursor")):
        return HttpResponse("Choose one value per inbox filter.", status=422)
    try:
        scope = reader.session_read_scope(request.user, workspace.pk)
        rows = reader.list_conversations(
            scope,
            social_account_id=request.GET.get("account") or None,
            platform=request.GET.get("platform") or None,
            workflow_state=request.GET.get("workflow") or None,
            search=request.GET.get("q", "").strip(),
            cursor=request.GET.get("cursor") or None,
        )
        accounts = reader.available_accounts(scope)
    except reader.CanonicalReadError as exc:
        return _error(exc)
    platforms = {account["platform"] for account in accounts}
    params = request.GET.copy()
    params.pop("page", None)
    params["cursor"] = rows["next_cursor"] or ""
    context = {
        "workspace": workspace,
        "canonical_mode": True,
        "inbox_domain": "dm",
        "canonical_rows": rows["conversations"],
        "canonical_accounts": accounts,
        "list_cursor": request.GET.get("cursor", ""),
        "platform_choices": [
            (value, label) for value, label in SocialAccount._meta.get_field("platform").choices if value in platforms
        ],
        "active_account": request.GET.get("account", ""),
        "active_platform": request.GET.get("platform", ""),
        "active_workflow": request.GET.get("workflow", ""),
        "search_query": request.GET.get("q", ""),
        "canonical_next_url": reverse("inbox:feed", kwargs={"workspace_id": workspace.pk}) + "?" + params.urlencode()
        if rows["next_cursor"]
        else "",
    }
    response = render(
        request, "inbox/partials/_canonical_list.html" if request.htmx else "inbox/canonical_feed.html", context
    )
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_permission("use_inbox")
@require_GET
@never_cache
def detail(request, workspace_id, conversation_id):
    from .views import _get_workspace

    if not enabled():
        return HttpResponse("Not found.", status=404)
    workspace = _get_workspace(request, workspace_id)
    reader = _reader()
    try:
        result = reader.read_conversation(
            reader.session_read_scope(request.user, workspace.pk),
            conversation_id,
            cursor=request.GET.get("cursor") or None,
        )
        if request.GET.get("fragment") == "history":
            return render(
                request,
                "inbox/partials/_canonical_history_response.html",
                _panel_context(request, workspace, result),
            )
        context = _panel_context(request, workspace, result)
    except reader.CanonicalReadError as exc:
        return _error(exc)
    return render(
        request, "inbox/partials/_canonical_panel.html" if request.htmx else "inbox/canonical_detail.html", context
    )


@login_required
@require_permission("use_inbox")
@require_POST
@never_cache
def acknowledge_read(request, workspace_id, conversation_id):
    from .views import _get_workspace

    if not enabled():
        return HttpResponse("Not found.", status=404)
    workspace = _get_workspace(request, workspace_id)
    reader = _reader()
    try:
        scope = reader.session_read_scope(request.user, workspace.pk)
        result = reader.acknowledge_read(scope, conversation_id, request.POST.get("read_ack_token", ""))
        result["unread_count"] = reader.unread_conversation_count(scope)
    except reader.CanonicalReadError as exc:
        return _error(exc)
    return JsonResponse(result)


def _render_action(request, workspace, identifier, *, part, **extra):
    reader = _reader()
    result = reader.read_conversation(reader.session_read_scope(request.user, workspace.pk), identifier)
    context = _panel_context(
        request,
        workspace,
        result,
        observation_token=result.get("composer_observation_token") or ""
        if part == "panel"
        else request.POST.get("composer_observation_token", ""),
    )
    context.update(extra)
    if part == "composer" and context.get("unsaved_draft_body"):
        active = context["composer_active_reply"]
        state = context["conversation_composer"]
        if str(state["composer_revision"]) == request.POST.get("composer_revision") and (
            active is None or str(active.action_nonce) == request.POST.get("composer_action_nonce")
        ):
            context["composer_body"] = context["unsaved_draft_body"]
            context["unsaved_draft_body"] = ""
            context["unsaved_in_editor"] = True
    if part != "panel":
        # The displayed history did not change. Never mint acknowledgement of unseen activity.
        context["composer_observation_token"] = request.POST.get("composer_observation_token", "")
        context["conversation_composer"]["scope_token"] = request.POST.get("composer_scope_token", "")
    response = render(request, f"inbox/partials/_canonical_{part}.html", context)
    response["HX-Retarget"] = "#inbox-detail-panel" if part == "panel" else f"#inbox-canonical-{part}"
    response["HX-Reswap"] = "innerHTML" if part == "panel" else "outerHTML"
    response["HX-Trigger"] = "inbox:refresh"
    response["Cache-Control"] = "private, no-store"
    return response


def _submit(request, workspace_id, conversation_id, *, send):
    from . import conversation_composer as composer
    from .services import ReplyStateError, reply_failure_reason
    from .views import _get_workspace

    if not enabled():
        return HttpResponse("Not found.", status=404)
    workspace = _get_workspace(request, workspace_id)
    reader = _reader()
    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Enter a message.", status=400)
    try:
        result = reader.read_conversation(reader.session_read_scope(request.user, workspace.pk), conversation_id)
        target = _panel_context(request, workspace, result)["canonical_target"]
    except reader.CanonicalReadError as exc:
        return _error(exc)
    try:
        params = dict(
            message=target,
            body=form.cleaned_data["body"],
            action_nonce=request.POST.get("composer_action_nonce", ""),
            expected_revision=int(request.POST.get("composer_revision", "")),
            scope_token=request.POST.get("composer_scope_token", ""),
            author=request.user,
            adopt_reply_id=request.POST.get("adopt_reply_id") or None,
            quote_target_id=request.POST.get("quote_target_id") or None,
        )
        if send:
            composer.send_conversation_reply(
                **params,
                actor=request.user,
                observation_token=request.POST.get("composer_observation_token", ""),
                draft_authorization=composer.session_read_authorization(request.user),
                authorization=session_send_authorization(request.user),
            )
        else:
            composer.save_conversation_draft(**params, authorization=composer.session_read_authorization(request.user))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, (ReplyStateError, DMSendGateError)) else reply_failure_reason(exc)
        if getattr(exc, "code", "") in {"stale_observation", "scope_changed"}:
            reason = "Select Latest before sending."
        try:
            nonce = UUID(request.POST.get("composer_action_nonce", ""))
        except (ValueError, TypeError):
            nonce = None
        saved_reply = InboxReply.objects.filter(conversation=target, action_nonce=nonce).first() if nonce else None
        saved = saved_reply is not None and saved_reply.body == form.cleaned_data["body"].strip()
        requested_quote = request.POST.get("quote_target_id", "")
        try:
            quote_id = UUID(requested_quote) if requested_quote else None
            unsaved_quote = quote_id != (saved_reply.quote_target_id if saved_reply else None)
        except (ValueError, TypeError):
            unsaved_quote = True
        try:
            response = _render_action(
                request,
                workspace,
                conversation_id,
                part="composer",
                composer_error=reason,
                needs_latest=getattr(exc, "code", "") in {"stale_observation", "scope_changed"},
                unsaved_draft_body="" if saved else form.cleaned_data["body"],
                unsaved_quote_selection=unsaved_quote,
            )
        except reader.CanonicalReadError as changed:
            return _error(changed)
        response["HX-Reply-Failed"] = "1"
        return response
    try:
        return _render_action(request, workspace, conversation_id, part="panel" if send else "composer")
    except reader.CanonicalReadError as exc:
        return _error(exc)


@login_required
@require_permission("use_inbox")
@require_POST
@never_cache
def save_draft(request, workspace_id, conversation_id):
    return _submit(request, workspace_id, conversation_id, send=False)


@login_required
@require_permission("reply_from_inbox")
@require_POST
@never_cache
def send_reply(request, workspace_id, conversation_id):
    return _submit(request, workspace_id, conversation_id, send=True)


@login_required
@require_permission("use_inbox")
@require_POST
@never_cache
def retire_failed(request, workspace_id, conversation_id):
    from .conversation_composer import retire_failed_conversation_reply, session_read_authorization
    from .views import _get_workspace

    if not enabled():
        return HttpResponse("Not found.", status=404)
    workspace = _get_workspace(request, workspace_id)
    reader = _reader()
    try:
        result = reader.read_conversation(reader.session_read_scope(request.user, workspace.pk), conversation_id)
        target = _panel_context(request, workspace, result)["canonical_target"]
        retire_failed_conversation_reply(
            message=target,
            reply_id=request.POST.get("reply_id"),
            expected_revision=int(request.POST.get("composer_revision", "")),
            scope_token=request.POST.get("composer_scope_token", ""),
            authorization=session_read_authorization(request.user),
        )
        return _render_action(request, workspace, conversation_id, part="composer")
    except reader.CanonicalReadError as exc:
        return _error(exc)
    except (ValueError, TypeError):
        return HttpResponse("The reply changed or still needs delivery review. Reopen this conversation.", status=409)


@login_required
@require_permission("reply_from_inbox")
@require_POST
@never_cache
def mark_done(request, workspace_id, conversation_id):
    from .conversation_workflow import mark_conversation_done
    from .views import _get_workspace

    if not enabled():
        return HttpResponse("Not found.", status=404)
    workspace = _get_workspace(request, workspace_id)
    reader = _reader()
    try:
        result = reader.read_conversation(reader.session_read_scope(request.user, workspace.pk), conversation_id)
        target = _panel_context(request, workspace, result)["canonical_target"]
        mark_conversation_done(
            conversation=target,
            expected_generation=int(request.POST.get("incoming_generation", "")),
            expected_revision=int(request.POST.get("conversation_revision", "")),
            confirm_order_uncertain=request.POST.get("confirm_order_uncertain") == "true",
            authorization=session_send_authorization(request.user),
        )
        return _render_action(request, workspace, conversation_id, part="header")
    except reader.CanonicalReadError as exc:
        return _error(exc)
    except (ValueError, TypeError):
        return HttpResponse("Conversation activity changed. Reload before marking it done.", status=409)


def legacy_detail(request, workspace, message):
    if message.message_type != "dm":
        return None
    from .canonical_compat import hold_legacy_fallback
    from .canonical_send_target import is_transport_projection

    reader = _reader()
    scope = reader.session_read_scope(request.user, workspace.pk)
    try:
        hold_legacy_fallback(scope, social_account_id=message.social_account_id)
        if enabled():
            identifier = reader.resolve_legacy_conversation(scope, message.pk)
            return detail(request, workspace.pk, identifier)
    except reader.CanonicalReadError as exc:
        return _error(exc)
    if is_transport_projection(message):
        return HttpResponse("Open the saved conversation to view this message.", status=409)
    return None


def hold_legacy_action(request, workspace, message):
    if message.message_type != "dm":
        return None
    from .canonical_compat import hold_legacy_fallback
    from .canonical_send_target import is_transport_projection

    reader = _reader()
    try:
        hold_legacy_fallback(
            reader.session_read_scope(request.user, workspace.pk), social_account_id=message.social_account_id
        )
    except reader.CanonicalReadError as exc:
        return _error(exc)
    if enabled() or is_transport_projection(message):
        return HttpResponse("Use the conversation controls for this message.", status=409)
    return None


@login_required
@require_permission("use_inbox")
@require_POST
@never_cache
def retire_draft(request, workspace_id, conversation_id):
    from .conversation_composer import retire_unattempted_conversation_draft, session_read_authorization
    from .views import _get_workspace

    if not enabled():
        return HttpResponse("Not found.", status=404)
    workspace = _get_workspace(request, workspace_id)
    reader = _reader()
    try:
        result = reader.read_conversation(reader.session_read_scope(request.user, workspace.pk), conversation_id)
        target = _panel_context(request, workspace, result)["canonical_target"]
        retire_unattempted_conversation_draft(
            message=target,
            reply_id=request.POST.get("reply_id"),
            expected_revision=int(request.POST.get("composer_revision", "")),
            scope_token=request.POST.get("composer_scope_token", ""),
            authorization=session_read_authorization(request.user),
        )
        return _render_action(request, workspace, conversation_id, part="composer")
    except reader.CanonicalReadError as exc:
        return _error(exc)
    except (ValueError, TypeError):
        return HttpResponse("The draft changed. Reload this conversation before starting a new draft.", status=409)


@login_required
@require_permission("use_inbox")
@require_GET
@never_cache
def pending_history(request, workspace_id, conversation_id):
    from .pending_history import page
    from .views import _get_workspace

    if not enabled():
        return HttpResponse("Not found.", status=404)
    workspace = _get_workspace(request, workspace_id)
    reader = _reader()
    try:
        scope = reader.session_read_scope(request.user, workspace.pk)
        result = reader.read_conversation(scope, conversation_id)
        context = _panel_context(request, workspace, result)
        context.update(
            page(
                request,
                workspace,
                context["canonical_target"],
                active=context["composer_active_reply"],
                cursor=request.GET.get("cursor"),
            )
        )
    except reader.CanonicalReadError as exc:
        return _error(exc)
    return render(request, "inbox/partials/_canonical_pending.html", context)
