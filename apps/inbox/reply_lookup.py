"""One manager-requested receipt lookup, with no history capture or writes."""

from datetime import timedelta
from urllib.parse import quote

from django.db import transaction
from django.utils import timezone

from providers import get_provider
from providers.meta_inbox_content import classify_conversation_identity
from providers.meta_messaging import resolve_recipient_id

from .locking import lock_dm_account
from .models import InboxMessage, InboxReply
from .presentation import native_thread_id, thread_id
from .reply_reconciliation import _aware_timestamp, _eligible, _error

_FIELDS = "id,participants{id},messages.limit(100){id,message,from{id},to{id},created_time}"
_UNCONFIRMED = (
    "This limited platform check could not confirm delivery. Missing results do not prove that nothing was sent."
)


def _selected(reply, actor, expected, expected_generation):
    original = reply.inbox_message
    account = lock_dm_account(original.social_account_id, original.workspace_id)
    current = InboxReply.objects.select_for_update().filter(pk=reply.pk).first()
    if account is None or current is None or current.inbox_message_id != original.pk:
        raise _error("stale", "The selected reply or account changed; reload before checking.")
    message = InboxMessage.objects.select_for_update().get(pk=current.inbox_message_id)
    current.inbox_message = message
    message.social_account = account
    _eligible(current, account, message, actor)
    if (
        expected is None
        or expected != current.updated_at
        or type(expected_generation) is not int
        or expected_generation < 0
        or current.send_generation != expected_generation
    ):
        raise _error("stale", "The reply changed after you opened it. Reload and review the current result.")
    return account, message, current


def _identity(account, message, reply):
    return (
        account.workspace_id,
        account.platform,
        account.account_platform_id,
        account.oauth_access_token,
        message.pk,
        message.platform_message_id,
        message.extra,
        message.sender_handle,
        reply.body,
        reply.updated_at,
        reply.send_generation,
    )


def _candidates(data, *, account, message, reply, native_id):
    if not isinstance(data, dict) or data.get("id") != native_id:
        return [], False
    extra = message.extra if isinstance(message.extra, dict) else {}
    peer = resolve_recipient_id(extra)
    if not peer or (message.sender_handle and message.sender_handle != peer):
        return [], False
    kind, _reason, returned_peer = classify_conversation_identity(
        {"participants": data.get("participants")}, own_ids=[account.account_platform_id], sender_id=peer
    )
    if kind != "direct" or returned_peer != peer:
        return [], False
    page = data.get("messages")
    if not isinstance(page, dict) or not isinstance(page.get("data"), list):
        return [], False
    rows = page["data"]
    more = len(rows) > 100 or bool(isinstance(page.get("paging"), dict) and page["paging"].get("next"))
    found = {}
    earliest = max(reply.created_at, message.received_at) - timedelta(seconds=1)
    # This is a candidate for human inspection, not proof that this invocation
    # sent it. Never inspect arbitrary old matches or infer absence as failure.
    latest = min(timezone.now(), reply.updated_at + timedelta(minutes=5))
    for row in rows[:100]:
        if not isinstance(row, dict) or row.get("message") != reply.body:
            continue
        sender = row.get("from")
        recipients = row.get("to")
        stamp = _aware_timestamp(row.get("created_time"))
        mid = native_thread_id(row.get("id"))
        to_rows = recipients.get("data") if isinstance(recipients, dict) else None
        to_paging = recipients.get("paging") if isinstance(recipients, dict) else None
        if (
            not isinstance(sender, dict)
            or sender.get("id") != account.account_platform_id
            or not isinstance(recipients, dict)
            or not isinstance(to_rows, list)
            or len(to_rows) != 1
            or not isinstance(to_rows[0], dict)
            or to_rows[0].get("id") != peer
            or (isinstance(to_paging, dict) and to_paging.get("next"))
            or not mid
            or stamp is None
            or not earliest <= stamp <= latest
        ):
            continue
        found[mid] = {"platform_reply_id": mid, "sent_at": stamp.isoformat()}
    candidates = sorted(found.values(), key=lambda row: (row["sent_at"], row["platform_reply_id"]), reverse=True)
    return candidates[:5], more or len(candidates) > 5


def lookup_reply_receipts(*, reply, actor, expected_updated_at, expected_send_generation):
    """Check one already-known native thread only, without saving its content."""
    expected = _aware_timestamp(expected_updated_at)
    with transaction.atomic():
        account, message, current = _selected(reply, actor, expected, expected_send_generation)
        before = _identity(account, message, current)
        native_id = thread_id(message)
        supported = account.platform in {"facebook", "instagram_login"} and account.connection_status == "connected"
        if not supported or not native_id or any(char in native_id for char in "/\\?#%"):
            return {
                "status": "unconfirmed",
                "reason": _UNCONFIRMED,
                "candidates": [],
                "more_available": False,
                "checked_at": timezone.now().isoformat(),
            }
    # No lock is kept over a read request. Recheck current scope and authority
    # before any result leaves this function. No token refresh or retry occurs.
    data = None
    try:
        from apps.publisher.engine import _resolve_publish_credentials
        from providers.facebook import BASE_URL
        from providers.instagram_login import API_BASE

        provider = get_provider(account.platform, _resolve_publish_credentials(account))
        base = BASE_URL if account.platform == "facebook" else API_BASE
        response = provider._request(
            "GET",
            f"{base}/{quote(native_id, safe='')}",
            access_token=account.oauth_access_token,
            params={"fields": _FIELDS},
        )
        data = response.json()
    except Exception:
        # Do not reflect provider diagnostics, tokens, or remote body in the UI.
        pass
    with transaction.atomic():
        account, message, current = _selected(reply, actor, expected, expected_send_generation)
        if _identity(account, message, current) != before:
            raise _error("stale", "The conversation or account changed during the check; reload before reviewing.")
        candidates, more = _candidates(data, account=account, message=message, reply=current, native_id=native_id)
    return {
        "status": "candidates" if candidates else "unconfirmed",
        "reason": "Matching receipt candidates were found. Verify the matching platform message before recording a result."
        if candidates
        else _UNCONFIRMED,
        "candidates": candidates,
        "more_available": more,
        "checked_at": timezone.now().isoformat(),
    }
