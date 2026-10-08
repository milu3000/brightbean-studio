"""Operational HTML fallback and preserved original history, entirely offline."""

import html
import re
from copy import copy
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inbox import canonical_reads as reader
from apps.inbox import preserved_history
from apps.inbox.models import ConversationMessage, ConversationReadState, InboxConversation, InboxMessage, InboxReply
from apps.inbox.tests.test_canonical_reads_rebuilt import context as _context
from apps.inbox.tests.test_canonical_reads_rebuilt import legacy, proof, row
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount

context = _context
pytestmark = pytest.mark.django_db


@pytest.fixture
def browser(context):
    client = Client()
    client.force_login(context.user)
    return client


def url(context, kind="basic_feed", **kwargs):
    return reverse("inbox:" + kind, kwargs={"workspace_id": context.account.workspace_id, **kwargs})


def get(browser, path, params=None):
    return browser.get(path, params or {}, secure=True)


def original(context, *, body="PRESERVED ORIGINAL", **overrides):
    values = dict(
        workspace=context.account.workspace,
        social_account=context.account,
        message_type="dm",
        platform_message_id="",
        sender_name="Original sender",
        body=body,
        received_at=timezone.now() - timedelta(days=450),
        extra={},
    )
    values.update(overrides)
    return InboxMessage.objects.create(**values)


def link(response, label):
    matches = re.findall(r'<a href="([^"]+)">' + re.escape(label) + r"</a>", response.content.decode())
    assert matches, response.content.decode()
    return html.unescape(matches[0])


def test_recovery_reads_new_history_with_sync_paused_and_all_business_state_unchanged(context, browser, settings):
    incoming = row(context, body="NEW CANONICAL INCOMING", direction="inbound", sender_id="peer", incoming_generation=1)
    anchor = legacy(context, incoming)
    connection_state = proof(context, incoming)
    outgoing = row(context, body="NEW CANONICAL OUTGOING")
    proof(context, outgoing)
    type(connection_state).objects.filter(pk=connection_state.pk).update(bootstrap_baseline_at=timezone.now())
    settings.INBOX_CONVERSATION_COMPOSER_ENABLED = False
    settings.INBOX_CONVERSATION_WORKFLOW_ENABLED = False
    draft = InboxReply.objects.create(inbox_message=anchor, body="SAVED DRAFT", status="draft")
    receipt = InboxReply.objects.create(
        inbox_message=anchor, body="SAVED RECEIPT", status="sent", sent_at=timezone.now()
    )
    before = list(InboxReply.objects.values())
    preference = context.user.last_workspace_id
    with (
        CaptureQueriesContext(connection) as queries,
        patch("apps.inbox.native_thread_reads.read_native_thread") as native,
    ):
        feed = get(browser, url(context))
        detail = get(browser, url(context, "basic_detail", conversation_id=context.conversation.pk))
    assert feed.status_code == detail.status_code == 200
    text = detail.content.decode()
    assert "NEW CANONICAL INCOMING" in text and "NEW CANONICAL OUTGOING" in text
    assert "STALE LEGACY BODY" not in text
    assert list(InboxReply.objects.values()) == before
    assert set(InboxReply.objects.values_list("pk", flat=True)) == {draft.pk, receipt.pk}
    assert not ConversationReadState.objects.exists()
    context.user.refresh_from_db()
    assert context.user.last_workspace_id == preference
    writes = [item["sql"] for item in queries if item["sql"].lstrip().split()[0] in {"UPDATE", "INSERT", "DELETE"}]
    # Django may renew the authenticated session expiry on ordinary GETs.
    # Recovery does not mutate inbox/account/workflow or navigation preference.
    assert all('UPDATE "django_session"' in statement for statement in writes)
    native.assert_not_called()
    assert "<script" not in text and "hx-" not in text and "read_ack_token" not in text
    assert detail["Cache-Control"].endswith("no-store") or "no-store" in detail["Cache-Control"]
    assert detail["Referrer-Policy"] == "no-referrer"


def test_outgoing_only_basic_detail_has_no_synthetic_incoming(context, browser):
    row(context, body="OUTBOUND ONLY")
    response = get(browser, url(context, "basic_detail", conversation_id=context.conversation.pk))
    assert response.status_code == 200 and "OUTBOUND ONLY" in response.content.decode()
    assert not InboxMessage.objects.exists()


def test_account_platform_search_are_applied_before_paging(context, browser, enroll_conversation_accounts):
    row(context, body="SEARCH OUTGOING")
    row(context, body="SEARCH INCOMING", direction="inbound", sender_name="Named customer")
    other = SocialAccount.objects.create(
        workspace=context.account.workspace,
        platform="instagram_login",
        account_platform_id="second",
        account_name="SECOND ACCOUNT",
        oauth_access_token="synthetic",
    )
    enroll_conversation_accounts(other, read=True)
    second_context = copy(context)
    second_context.account = other
    second_context.conversation = InboxConversation.objects.create(
        workspace=other.workspace,
        social_account=other,
        platform=other.platform,
        platform_conversation_id="other-thread",
        peer_id="other-peer",
        conversation_type="direct",
    )
    row(second_context, body="OTHER ACCOUNT BODY")
    for term in ["SEARCH OUTGOING", "SEARCH INCOMING", "Named customer"]:
        response = get(browser, url(context), {"q": term})
        assert response.status_code == 200
        assert url(context, "basic_detail", conversation_id=context.conversation.pk) in response.content.decode()
        assert "OTHER ACCOUNT BODY" not in response.content.decode()
    response = get(browser, url(context), {"platform": "instagram_login", "account": str(other.pk)})
    assert "OTHER ACCOUNT BODY" in response.content.decode() and "SEARCH INCOMING" not in response.content.decode()


def test_plain_older_newest_and_undated_links(context, browser):
    now = timezone.now()
    for index in range(23):
        row(context, body=f"DATED {index:02d}", occurred_at=now - timedelta(minutes=index))
    for index in range(12):
        row(context, body=f"UNDATED {index:02d}", occurred_at=None)
    path = url(context, "basic_detail", conversation_id=context.conversation.pk)
    response = get(browser, path)
    assert response.status_code == 200
    older = get(browser, link(response, "Older"))
    assert "DATED 22" in older.content.decode() and "DATED 00" not in older.content.decode()
    assert link(older, "Newest") == path
    undated = get(browser, link(response, "More without time"))
    assert "UNDATED" in undated.content.decode() and "Time unavailable" in undated.content.decode()
    assert not ConversationReadState.objects.exists()


@pytest.mark.parametrize("reason", ["withdrawn", "expired"])
def test_both_basic_surfaces_redact_canonical_shadow_even_without_link(context, browser, reason):
    message = row(
        context,
        body="PRIVATE CANONICAL",
        direction="inbound",
        attachments=[{"type": "image", "url": "https://example.com/PRIVATE-MEDIA"}],
    )
    anchor = legacy(context, message)
    proof(
        context,
        message,
        **(
            {"withdrawn_at": timezone.now(), "retained_body": "PRIVATE RETAINED"}
            if reason == "withdrawn"
            else {"expired_at": timezone.now() - timedelta(seconds=1)}
        ),
    )
    ConversationMessage.objects.filter(pk=message.pk).update(legacy_message=None)
    for path in [
        url(context),
        url(context, "basic_detail", conversation_id=context.conversation.pk),
        url(context, "basic_preserved_feed"),
        url(context, "basic_preserved_detail", message_id=anchor.pk),
    ]:
        response = get(browser, path)
        assert response.status_code == 200
        assert "PRIVATE" not in response.content.decode() and "STALE LEGACY" not in response.content.decode()
        assert ("Message withdrawn" if reason == "withdrawn" else "Content expired") in response.content.decode()
    result = preserved_history.list_records(context.scope, search="STALE LEGACY")
    assert result["records"] == []
    assert preserved_history.list_records(context.scope, search="PRIVATE")["records"] == []


def test_held_missing_identity_old_history_stays_human_readable_without_new_retention(context, browser):
    old = original(context)
    response = get(browser, url(context, "basic_preserved_feed"))
    assert response.status_code == 200 and "PRESERVED ORIGINAL" in response.content.decode()
    detail = get(browser, url(context, "basic_preserved_detail", message_id=old.pk))
    assert detail.status_code == 200 and "Unlinked record" in detail.content.decode()
    assert str(old.received_at.year) in detail.content.decode()
    assert not InboxConversation.objects.exclude(pk=context.conversation.pk).exists()
    assert not ConversationMessage.objects.exists()
    with pytest.raises(reader.CanonicalReadError):
        preserved_history.read_record(reader.key_read_scope(context.key.api_key), old.pk)


@pytest.mark.parametrize("change", ["account", "workspace", "platform", "generation"])
def test_canonical_shadow_rebind_never_resurrects_original_body(context, browser, change, organization):
    message = row(context, direction="inbound", body="CANONICAL PRIVATE")
    anchor = legacy(context, message)
    binding = proof(context, message)
    if change == "account":
        other = SocialAccount.objects.create(
            workspace=context.account.workspace, platform=context.account.platform, account_platform_id="other"
        )
        ConversationMessage.objects.filter(pk=message.pk).update(social_account=other)
    elif change == "workspace":
        from apps.workspaces.models import Workspace

        foreign = Workspace.objects.create(name="SECRET FOREIGN WORKSPACE", organization=organization)
        ConversationMessage.objects.filter(pk=message.pk).update(workspace=foreign)
    elif change == "platform":
        ConversationMessage.objects.filter(pk=message.pk).update(legacy_message=None)
        SocialAccount.objects.filter(pk=context.account.pk).update(platform="instagram_login")
    else:
        type(binding).objects.filter(pk=binding.pk).update(generation=uuid4())
    response = get(browser, url(context, "basic_preserved_detail", message_id=anchor.pk))
    assert response.status_code == 200
    assert "Content unavailable" in response.content.decode()
    assert "PRIVATE" not in response.content.decode() and "STALE LEGACY" not in response.content.decode()


@pytest.mark.parametrize("path_kind", ["basic_feed", "basic_preserved_feed"])
@pytest.mark.parametrize("change", ["membership", "permission", "read_flag", "v2_flag"])
def test_recovery_current_permissions_and_enabled_reader_are_required(context, browser, settings, path_kind, change):
    row(context, body="CANONICAL PRIVATE")
    original(context, body="PRESERVED PRIVATE")
    if change == "membership":
        context.member.delete()
    elif change == "permission":
        WorkspaceMembership.objects.filter(pk=context.member.pk).update(workspace_role="viewer")
    elif change == "read_flag":
        settings.INBOX_CANONICAL_READ_ENABLED = False
    else:
        settings.INBOX_CONVERSATION_V2_ENABLED = False
    response = get(browser, url(context, path_kind))
    if change == "v2_flag" and path_kind == "basic_feed":
        # Empty eligible scope remains truthful; no legacy fallback is returned.
        assert response.status_code in {200, 404, 409}
    else:
        assert response.status_code in {403, 404, 409}
    assert "PRIVATE" not in response.content.decode()


def test_recovery_rejects_foreign_account_and_authenticates_get_only(context, browser, organization):
    row(context)
    foreign = uuid4()
    response = get(browser, url(context), {"account": str(foreign)})
    assert response.status_code == 404
    assert browser.post(url(context), secure=True).status_code == 405
    assert get(Client(), url(context)).status_code == 302


def test_preserved_search_page_pins_current_grants_and_is_read_only(context, browser):
    for index in range(22):
        original(context, body=f"PRESERVED {index}", platform_message_id=f"legacy-{index}")
    first = get(browser, url(context, "basic_preserved_feed"))
    older = link(first, "Older")
    next_page = get(browser, older)
    assert next_page.status_code == 200 and next_page.context["page"]["records"]
    assert get(browser, url(context, "basic_preserved_feed"), {"q": "PRESERVED 21"}).status_code == 200
    context.member.delete()
    assert get(browser, older).status_code in {403, 404}
    assert not ConversationReadState.objects.exists()


def test_escaping_and_safe_links_no_media_embedding(context, browser):
    row(
        context,
        body='<script>alert("body")</script>',
        attachments=[{"type": "share", "title": "SAFE LINK", "url": "https://example.com/post"}],
    )
    response = get(browser, url(context, "basic_detail", conversation_id=context.conversation.pk))
    text = response.content.decode()
    assert "&lt;script&gt;" in text and "<script>" not in text
    assert 'rel="noopener noreferrer"' in text and "<img" not in text


def test_revocation_during_basic_projection_never_releases_history(context, browser):
    row(context, body="PRIVATE")
    project = reader.project_message
    revoked = False

    def revoke(message):
        nonlocal revoked
        value = project(message)
        if not revoked:
            WorkspaceMembership.objects.filter(pk=context.member.pk).delete()
            revoked = True
        return value

    with patch.object(reader, "project_message", side_effect=revoke):
        response = get(browser, url(context, "basic_detail", conversation_id=context.conversation.pk))
    assert response.status_code == 404 and "PRIVATE" not in response.content.decode()


def test_preserved_transport_stub_is_not_an_original_record(context, browser):
    stub = original(context, extra={"transport_projection": True, "canonical_message_id": str(uuid4())})
    assert preserved_history.list_records(context.scope)["records"] == []
    assert get(browser, url(context, "basic_preserved_detail", message_id=stub.pk)).status_code == 404


def test_public_mention_filter_rest_mcp_keeps_facets_out_of_dm(context):
    from apps.inbox.tests.test_canonical_adapters_rebuilt import client_for, rpc

    mention = InboxMessage.objects.create(
        workspace=context.account.workspace,
        social_account=context.account,
        message_type="comment",
        platform_message_id="mentioned-comment",
        body="COMMENT MENTION",
        received_at=timezone.now(),
        extra={"is_mention": True},
    )
    original(context, body="PRIVATE DM", platform_message_id="dm-mention", extra={"is_mention": True})
    result = client_for(context).get("/api/v1/inbox/", {"message_type": "mention"}, secure=True)
    assert result.status_code == 200 and [item["id"] for item in result.json()["messages"]] == [str(mention.pk)]
    value = rpc(context, "list_inbox_messages", {"message_type": "mention"})
    assert [item["id"] for item in value["messages"]] == [str(mention.pk)]


@pytest.mark.parametrize("setting", ["INBOX_CONVERSATION_V2_ENABLED", "INBOX_CONVERSATION_V2_READ_ACCOUNTS"])
def test_owned_account_read_enrollment_loss_is_explicit_hold(context, browser, settings, setting):
    message = row(context, body="CANONICAL PRIVATE")
    proof(context, message)
    setattr(settings, setting, False if setting.endswith("ENABLED") else [])
    response = get(browser, url(context))
    assert response.status_code == 409 and "History unavailable" in response.content.decode()
    assert "CANONICAL PRIVATE" not in response.content.decode()
