"""DM identity, pagination and read-only rollups; no real provider calls."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox.conversations import ConversationIndex, inbox_page
from apps.inbox.models import InboxMessage, InboxReply, InternalNote
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def dm(inbox_account):
    def create(*, sender="peer-1", native=None, account=None, extra=None, **kwargs):
        account = account or inbox_account
        payload = {"sender_id": sender} if sender else {}
        if native is not None:
            payload["conversation_id"] = native
        if extra is not None:
            payload = extra
        return InboxMessage.objects.create(
            workspace=account.workspace,
            social_account=account,
            platform_message_id=str(uuid.uuid4()),
            sender_name="Same name",
            sender_handle=sender or "same-name",
            extra=payload,
            **{"message_type": "dm", "body": "hello", "received_at": timezone.now(), **kwargs},
        )

    return create


@pytest.fixture
def signed_in(client, inbox_workspace, org_owner, user):
    WorkspaceMembership.objects.create(workspace=inbox_workspace, user=user, workspace_role="owner")
    client.force_login(user)
    return client


def scope(workspace):
    return InboxMessage.objects.filter(workspace=workspace)


def detail(workspace, message):
    return f"/workspace/{workspace.id}/inbox/{message.id}/"


def test_native_and_webhook_fallback_join(dm, inbox_workspace):
    polled = dm(native="c-1")
    webhook = dm(extra={"sender": {"id": "peer-1"}, "message": {"mid": "m-2"}})
    index = ConversationIndex(scope(inbox_workspace))
    assert index.keys[polled.id] == index.keys[webhook.id]
    assert len(index.groups) == 1


def test_poll_backfill_enriches_identity_without_rewriting_ids(dm, inbox_workspace):
    first, second = dm(), dm()
    before = set(scope(inbox_workspace).values_list("id", flat=True))
    first.extra = {"sender_id": "peer-1", "conversation_id": "native"}
    first.save(update_fields=["extra"])
    index = ConversationIndex(scope(inbox_workspace))
    assert index.keys[first.id] == index.keys[second.id]
    assert set(scope(inbox_workspace).values_list("id", flat=True)) == before


def test_ambiguous_native_threads_do_not_swallow_sender_only_row(dm, inbox_workspace):
    one, two, unknown = dm(native="one"), dm(native="two"), dm()
    index = ConversationIndex(scope(inbox_workspace))
    assert len({index.keys[x.id] for x in (one, two, unknown)}) == 3


def test_native_multi_participant_thread_does_not_claim_fallback(dm, inbox_workspace):
    one = dm(native="group", sender="a")
    two = dm(native="group", sender="b")
    unknown = dm(sender="a")
    index = ConversationIndex(scope(inbox_workspace))
    assert index.keys[one.id] == index.keys[two.id]
    assert index.keys[unknown.id] != index.keys[one.id]


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"sender_id": True},
        {"sender_id": []},
        {"sender_id": ""},
        {"sender_id": "a", "sender": {"id": "b"}},
        {"conversation_id": {"bad": "shape"}},
    ],
)
def test_missing_malformed_or_conflicting_ids_never_merge_by_name(dm, inbox_workspace, extra):
    dm(extra=extra)
    dm(extra=extra)
    assert len(ConversationIndex(scope(inbox_workspace)).groups) == 2


def test_brand_platform_and_workspace_boundaries(dm, inbox_account, inbox_workspace, organization):
    other_ws = Workspace.objects.create(organization=organization, name="Other")
    accounts = [inbox_account]
    for platform, workspace in [
        ("facebook", inbox_workspace),
        ("instagram_login", inbox_workspace),
        ("facebook", other_ws),
    ]:
        accounts.append(
            SocialAccount.objects.create(
                workspace=workspace,
                platform=platform,
                account_platform_id=str(uuid.uuid4()),
                account_name="Other brand",
            )
        )
    messages = [dm(account=account, native="same-native", sender="same-peer") for account in accounts]
    index = ConversationIndex(scope(inbox_workspace))
    assert len(index.groups) == 3
    assert messages[-1].id not in index.keys


def test_sender_fallback_not_assumed_for_unknown_provider(dm, inbox_workspace, inbox_account):
    inbox_account.platform = "mastodon"
    inbox_account.save(update_fields=["platform"])
    dm()
    dm()
    assert len(ConversationIndex(scope(inbox_workspace)).groups) == 2


def test_grouping_precedes_pagination_and_comments_stay_individual(dm, inbox_workspace):
    for _ in range(55):
        dm(sender="busy")
    dm(sender="other")
    comment1 = dm(message_type="comment")
    comment2 = dm(message_type="comment")
    base = scope(inbox_workspace)
    rows, page = inbox_page(base, base, page_size=2)
    second, page2 = inbox_page(base, base, page_number=2, page_size=2)
    assert page.paginator.count == 4
    assert len(rows) == len(second) == 2
    assert page2.number == 2
    assert {r.message.id for r in rows} == {comment1.id, comment2.id}
    assert sorted(r.count for r in second) == [1, 55]


def test_filter_matching_old_member_keeps_full_conversation_summary(dm, inbox_workspace):
    dm(status="resolved", body="search needle")
    latest = dm(status="unread")
    base = scope(inbox_workspace)
    entries, _ = inbox_page(base, base.filter(body__icontains="needle"))
    assert len(entries) == 1
    row = entries[0]
    assert row.message.id == latest.id
    assert row.count == 2 and row.unread_count == 1 and row.pending_count == 1
    assert row.status == "unread"


def test_detail_merges_inbound_studio_replies_and_notes(dm, inbox_workspace, signed_in, user):
    first = dm(body="first incoming", received_at=timezone.now() - timedelta(hours=2))
    latest = dm(body="latest incoming", received_at=timezone.now() - timedelta(hours=1))
    sent = InboxReply.objects.create(
        inbox_message=first, author=user, body="Studio answer", status="sent", sent_at=timezone.now()
    )
    draft = InboxReply.objects.create(inbox_message=first, author=user, body="Pending draft")
    note = InternalNote.objects.create(inbox_message=latest, author=user, body="private note")
    response = signed_in.get(detail(inbox_workspace, first), HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert response.context["message"].id == latest.id
    assert {item.id for _, item, _ in response.context["thread"]} == {first.id, latest.id, sent.id, note.id}
    assert [d.id for d in response.context["draft_replies"]] == [draft.id]
    for message in (first, latest):
        message.refresh_from_db()
        assert message.status == "open"  # Reading is never resolution.
    assert b"Native-app replies and earlier history may be missing" in response.content
    assert b"Manage" in response.content
    assert InboxMessage.objects.count() == 2


def test_recent_outbound_to_old_inbound_is_on_newest_history_page(dm, inbox_workspace, signed_in):
    old = dm(received_at=timezone.now() - timedelta(days=10))
    for i in range(110):
        dm(received_at=timezone.now() - timedelta(minutes=120 - i))
    reply = InboxReply.objects.create(inbox_message=old, status="sent", sent_at=timezone.now(), body="fresh answer")
    response = signed_in.get(detail(inbox_workspace, old))
    assert response.context["history_page"].paginator.count == 112
    assert any(item.id == reply.id for _, item, _ in response.context["thread"])
    old.refresh_from_db()
    assert old.status == "unread"  # Not displayed on this event page.
    older = signed_in.get(detail(inbox_workspace, old) + "?history_page=2")
    assert any(item.id == old.id for _, item, _ in older.context["thread"])
    old.refresh_from_db()
    assert old.status == "open"


def test_single_message_management_never_resolves_siblings(dm, inbox_workspace, signed_in):
    first, second = dm(), dm()
    response = signed_in.get(detail(inbox_workspace, first) + "?single=1")
    assert response.context["conversation"] is None
    second.refresh_from_db()
    assert second.status == "unread"
    response = signed_in.post(detail(inbox_workspace, first) + "status/", {"status": "resolved", "single": "1"})
    assert response.status_code == 200
    assert response.context["conversation"] is None
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == "resolved" and second.status == "unread"


def test_feed_renders_one_dm_row_without_misleading_bulk_checkbox(dm, inbox_workspace, signed_in):
    dm()
    latest = dm()
    response = signed_in.get(f"/workspace/{inbox_workspace.id}/inbox/", HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert len(response.context["inbox_entries"]) == 1
    assert response.content.count(b'class="inbox-row ') == 1
    assert str(latest.id).encode() in response.content
    assert b"2 messages" in response.content
    assert b'type="checkbox"' not in response.content


def test_outside_workspace_detail_denied(dm, organization, signed_in, inbox_workspace):
    ws = Workspace.objects.create(organization=organization, name="Outside")
    account = SocialAccount.objects.create(workspace=ws, platform="facebook", account_platform_id="outside")
    message = dm(account=account)
    assert signed_in.get(detail(inbox_workspace, message)).status_code == 404
    assert signed_in.get(detail(ws, message)).status_code == 403


def test_new_outbound_bumps_old_conversation_in_feed(dm, inbox_workspace):
    old = dm(sender="old", received_at=timezone.now() - timedelta(days=10))
    dm(sender="recent")
    sent_at = timezone.now()
    InboxReply.objects.create(inbox_message=old, status="sent", sent_at=sent_at, body="new answer")
    InboxReply.objects.create(inbox_message=old, status="failed", body="failed should not count")
    base = scope(inbox_workspace)
    entries, _ = inbox_page(base, base)
    assert entries[0].message.id == old.id
    assert entries[0].last_activity == sent_at


def test_index_extracts_only_identity_fields_not_raw_payload(dm, inbox_workspace):
    dm(extra={"sender_id": "a", "message": {"text": "large body", "attachments": ["private"]}})
    index = ConversationIndex(scope(inbox_workspace))
    assert "extra" not in index.rows[0]
    assert "body" not in index.rows[0]
    assert index.rows[0]["scoped_id"] == "a"


def test_authorless_studio_records_render_safely(dm, inbox_workspace, signed_in):
    message = dm()
    InboxReply.objects.create(inbox_message=message, body="agent draft")
    InboxReply.objects.create(inbox_message=message, body="former member reply", status="sent", sent_at=timezone.now())
    InternalNote.objects.create(inbox_message=message, body="former member note")
    response = signed_in.get(detail(inbox_workspace, message))
    assert response.status_code == 200
    assert b"agent draft" in response.content
    assert b"former member reply" in response.content
    assert b"former member note" in response.content
