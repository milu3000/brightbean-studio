"""Adversarial saved-only unified feed evidence using synthetic isolated rows."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from django.core import signing
from django.db import connection
from django.db.models import F
from django.http import QueryDict
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.inbox import canonical_reads as reader
from apps.inbox import unified_feed as feed
from apps.inbox.models import (
    ConversationMessage,
    ConversationObservationState,
    ConversationReadState,
    ConversationSyncIdentity,
    InboxConversation,
    InboxMessage,
    InboxReply,
    InboxSyncConnection,
)
from apps.inbox.tests.test_canonical_reads_rebuilt import context as _context
from apps.inbox.tests.test_canonical_reads_rebuilt import proof, row
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

context = _context
pytestmark = pytest.mark.django_db


def selected(**values):
    parameters = QueryDict(mutable=True)
    parameters.update(values)
    return feed.filters(parameters)


def account(context, *, platform="facebook", workspace=None, **values):
    return SocialAccount.objects.create(
        workspace=workspace or context.account.workspace,
        platform=platform,
        account_platform_id=str(uuid4()),
        account_name="Synthetic legacy account",
        **values,
    )


def legacy(account, *, stamp=None, kind="comment", **values):
    defaults = {
        "workspace": account.workspace,
        "social_account": account,
        "platform_message_id": str(uuid4()),
        "message_type": kind,
        "sender_name": "Synthetic person",
        "body": "Public synthetic text",
        "received_at": stamp or timezone.now(),
    }
    defaults.update(values)
    return InboxMessage.objects.create(**defaults)


def conversation(context, *, stamp="now", **values):
    defaults = {
        "workspace": context.account.workspace,
        "social_account": context.account,
        "platform": context.account.platform,
        "platform_conversation_id": str(uuid4()),
        "identity_kind": "platform",
        "peer_id": "peer",
        "conversation_type": "direct",
        "classification_reason": "participants_pair",
    }
    defaults.update(values)
    thread = InboxConversation.objects.create(**defaults)
    message = row(SimpleNamespace(account=context.account, conversation=thread), occurred_at=stamp)
    return thread, message


def collect(context, *, filters=None, limit=7):
    rows, cursor = [], None
    while True:
        page = feed.read_feed(context.scope, filters or selected(), cursor=cursor, limit=limit)
        rows.extend(page["rows"])
        cursor = page["next_cursor"]
        if cursor is None:
            return rows
        assert len(rows) < 1000, "pagination did not terminate"


def test_global_mixed_pages_have_exact_coverage_ties_and_undated_last(context):
    other = account(context)
    now = timezone.now()
    expected = []
    for index in range(35):
        stamp = now - timedelta(minutes=index // 2)
        thread, _ = conversation(context, stamp=stamp)
        old = legacy(other, stamp=stamp, kind="dm", extra={"conversation_id": f"legacy-{index}"})
        expected.extend([(stamp, "canonical", str(thread.pk)), (stamp, "legacy", str(old.pk))])
    for _ in range(3):
        thread, _ = conversation(context, stamp=None)
        expected.append((datetime.min.replace(tzinfo=UTC), "canonical", str(thread.pk)))
    records = collect(context)
    actual = [
        (record["stamp"] or datetime.min.replace(tzinfo=UTC), record["source"], record["id"]) for record in records
    ]
    assert actual == sorted(expected, reverse=True)
    assert len({(record["source"], record["id"]) for record in records}) == 73
    assert all(record["timestamp"] is None for record in records[-3:])
    assert all(record["source"] == "canonical" for record in records[-3:])


def test_all_types_is_a_single_mixed_source_page_without_shadow_duplicates(context):
    saved = row(context, direction="inbound", sender_id="peer", body="Saved canonical incoming")
    shadow = legacy(
        context.account,
        kind="dm",
        body="DO NOT SHOW SHADOW",
        platform_message_id=saved.platform_message_id,
        extra={"conversation_id": context.conversation.platform_conversation_id},
    )
    saved.legacy_message = shadow
    saved.save(update_fields=["legacy_message"])
    legacy(context.account, kind="dm", extra={"transport_projection": True}, body="DO NOT SHOW TRANSPORT")
    legacy_only = account(context)
    old = legacy(legacy_only, kind="dm", body="Legacy-only private text")
    public = legacy(context.account, body="Public account comment")
    mention = legacy(legacy_only, kind="mention", body="A saved mention")
    review = legacy(legacy_only, kind="review", body="An existing saved review")
    records = collect(context)
    assert {(record["source"], record["id"]) for record in records} == {
        ("canonical", str(context.conversation.pk)),
        *(("legacy", str(item.pk)) for item in (old, public, mention, review)),
    }
    assert "DO NOT SHOW" not in repr(records)
    assert {record["message_type"] for record in records} == {"dm", "comment", "mention", "review"}


def test_held_account_never_reopens_dm_shadows_but_keeps_public_comments(context, settings):
    message = row(context)
    proof(context, message)
    private = legacy(context.account, kind="dm", body="FORBIDDEN HELD SHADOW")
    public = legacy(context.account, body="Existing public comment")
    settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    result = feed.read_feed(context.scope, selected())
    assert [record["id"] for record in result["rows"]] == [str(public.pk)]
    assert [source["id"] for source in result["unavailable_sources"]] == [str(context.account.pk)]
    assert private.body not in repr(result)
    assert feed.read_feed(context.scope, selected(domain="dm"))["rows"] == []


def test_account_platform_and_tenant_filters_never_broaden_scope(context):
    row(context)
    first, second = account(context), account(context, platform="youtube")
    first_record, second_record = legacy(first), legacy(second)
    foreign_workspace = Workspace.objects.create(
        name="Foreign tenant", organization=context.account.workspace.organization
    )
    foreign_account = account(context, workspace=foreign_workspace)
    legacy(foreign_account, body="FOREIGN TENANT")
    legacy(foreign_account, workspace=context.account.workspace, body="MISMATCHED ACCOUNT OWNER")
    legacy(first, workspace=foreign_workspace, body="MISMATCHED ROW OWNER")
    assert {record["id"] for record in collect(context, filters=selected(account=str(first.pk)))} == {
        str(first_record.pk)
    }
    assert {record["id"] for record in collect(context, filters=selected(platform="youtube"))} == {
        str(second_record.pk)
    }
    assert collect(context, filters=selected(account=str(first.pk), platform="youtube")) == []
    assert "FOREIGN" not in repr(collect(context)) and "MISMATCHED" not in repr(collect(context))
    with pytest.raises(reader.CanonicalReadError, match="unavailable"):
        feed.read_feed(context.scope, selected(account=str(foreign_account.pk)))


@pytest.mark.parametrize("fault", ["workspace", "account", "platform", "conversation_account"])
def test_canonical_messages_require_matching_tenant_account_platform_and_thread(context, fault):
    valid = row(context, body="Allowed saved body")
    malicious = row(context, body="FORBIDDEN MISMATCH", occurred_at=timezone.now() + timedelta(days=1))
    if fault == "workspace":
        foreign = Workspace.objects.create(name="Other", organization=context.account.workspace.organization)
        ConversationMessage.objects.filter(pk=malicious.pk).update(workspace=foreign)
    elif fault == "account":
        ConversationMessage.objects.filter(pk=malicious.pk).update(social_account=account(context))
    elif fault == "platform":
        ConversationMessage.objects.filter(pk=malicious.pk).update(platform="instagram_login")
    else:
        other, _ = conversation(context)
        InboxConversation.objects.filter(pk=other.pk).update(social_account=account(context))
        ConversationMessage.objects.filter(pk=malicious.pk).update(conversation=other)
    records = collect(context)
    assert "FORBIDDEN" not in repr(records)
    assert records[0]["canonical"]["latest_message"]["id"] == str(valid.pk)


@pytest.mark.parametrize("fault", ["native", "connection_generation", "message_generation"])
def test_durable_provenance_failure_does_not_fall_back_to_legacy(context, fault):
    message = row(context, body="FORBIDDEN STALE HISTORY")
    connection = proof(context, message)
    legacy(context.account, kind="dm", body="FORBIDDEN SHADOW")
    if fault == "native":
        SocialAccount.objects.filter(pk=context.account.pk).update(account_platform_id="new-native-owner")
        with pytest.raises(reader.CanonicalReadError):
            feed.read_feed(context.scope, selected())
        return
    if fault == "connection_generation":
        InboxSyncConnection.objects.filter(pk=connection.pk).update(generation=uuid4())
    else:
        ConversationObservationState.objects.filter(message=message).update(connection_generation=uuid4())
    assert feed.read_feed(context.scope, selected())["rows"] == []


def test_legacy_native_groups_are_exact_account_scoped_and_never_sender_inferred(context):
    first, second = account(context), account(context)
    old = legacy(first, kind="dm", extra={"conversation_id": "000042"})
    newer = legacy(first, kind="dm", extra={"conversation_id": "000042"})
    other = legacy(second, kind="dm", extra={"conversation_id": "000042"})
    unthreaded = [legacy(first, kind="dm", extra={"conversation_id": invalid}) for invalid in (42, "", None, "bad id")]
    records = collect(context, filters=selected(domain="dm"))
    assert {record["id"] for record in records} == {str(item.pk) for item in [newer, other, *unthreaded]}
    assert next(record for record in records if record["id"] == str(newer.pk))["matched_count"] == 2
    assert str(old.pk) not in {record["id"] for record in records}


def test_public_threads_preserve_exact_post_root_account_and_mention_facet(context):
    first, second = account(context), account(context)
    root = legacy(first, platform_message_id="root", extra={"post_id": "page_post", "parent_id": ""})
    child = legacy(first, extra={"post_id": "page_post", "parent_id": "root", "is_mention": True})
    separate = [
        legacy(first, extra={"post_id": "page_post", "parent_id": ""}),
        legacy(first, extra={"post_id": "otherpage_post", "parent_id": "root"}),
        legacy(second, extra={"post_id": "page_post", "parent_id": "root"}),
        legacy(first, extra={"post_id": "page_post", "root_comment_id": "root"}),
        legacy(first, kind="mention", extra={"reply_edge": "media", "post_id": "page_post"}),
    ]
    records = collect(context)
    assert {record["id"] for record in records} == {str(item.pk) for item in [child, *separate]}
    assert next(record for record in records if record["id"] == str(child.pk))["matched_count"] == 2
    assert str(root.pk) not in {record["id"] for record in records}
    mentions = collect(context, filters=selected(domain="mention"))
    assert {record["id"] for record in mentions} == {str(child.pk), str(separate[-1].pk)}
    comments = collect(context, filters=selected(domain="comment"))
    assert {record["id"] for record in comments} == {str(item.pk) for item in [child, *separate[:-1]]}


@pytest.mark.parametrize("marker", ["true", 1, False, None, [], {}])
def test_mention_filter_does_not_treat_truthy_json_values_as_true(context, marker):
    legacy(context.account, extra={"is_mention": marker})
    assert collect(context, filters=selected(domain="mention")) == []


def test_status_and_search_filter_matching_members_before_public_thread_grouping(context):
    root = legacy(
        context.account,
        status="unread",
        body="target old",
        platform_message_id="root",
        extra={"post_id": "full_post", "parent_id": ""},
    )
    legacy(context.account, status="resolved", body="other new", extra={"post_id": "full_post", "parent_id": "root"})
    for filters in (selected(domain="comment", status="unread"), selected(domain="comment", q="target")):
        records = collect(context, filters=filters)
        assert [record["id"] for record in records] == [str(root.pk)]
        assert records[0]["matched_count"] == 1


def test_search_covers_saved_outgoing_but_withholds_deleted_or_expired_bodies(context):
    row(context, direction="outbound", body="outbound unique needle")
    assert collect(context, filters=selected(q="unique needle"))[0]["id"] == str(context.conversation.pk)
    for status in ("removed", "expired"):
        row(context, body="PRIVATE HIDDEN TEXT", content_status=status)
    assert collect(context, filters=selected(q="PRIVATE HIDDEN")) == []
    old = legacy(account(context), kind="dm", body="PRIVATE OLD TEXT", extra={"is_deleted": True})
    assert collect(context, filters=selected(q="PRIVATE OLD")) == []
    assert old.body not in repr(collect(context))


def test_workflow_filters_do_not_relabel_legacy_or_public_messages(context):
    row(context)
    legacy(account(context), kind="dm")
    legacy(context.account)
    assert [record["id"] for record in collect(context, filters=selected(domain="dm", workflow="unclassified"))] == [
        str(context.conversation.pk)
    ]
    InboxConversation.objects.filter(pk=context.conversation.pk).update(workflow_state="waiting")
    assert collect(context, filters=selected(domain="dm", workflow="needs_action")) == []
    assert collect(context, filters=selected(domain="dm", workflow="waiting"))[0]["workflow_state"] == "waiting"


@pytest.mark.parametrize(
    "parameters",
    [
        "domain=unknown",
        "type=dm&domain=comment",
        "domain=all&status=unread",
        "domain=dm&status=bogus",
        "domain=comment&workflow=waiting",
        "domain=dm&workflow=bogus",
        "platform=bogus",
        "q=a&q=b",
        "account=a&account=b",
        "domain=dm&domain=comment",
        "cursor=a&cursor=b",
        "sentiment=positive",
        "view=mine",
    ],
)
def test_invalid_or_ambiguous_filters_fail_closed(parameters):
    with pytest.raises(reader.CanonicalReadError) as error:
        feed.filters(QueryDict(parameters))
    assert error.value.code == "invalid_filter"


@pytest.mark.parametrize("cursor", ["broken", "x" * 4097, 7, True, {}, ["bad"]])
def test_malformed_cursor_fails_closed(context, cursor):
    row(context)
    # Nonempty invalid objects must not silently become an initial page.
    if cursor == {}:
        cursor = {"bad": "cursor"}
    with pytest.raises(reader.CanonicalReadError) as error:
        feed.read_feed(context.scope, selected(), cursor=cursor)
    assert error.value.code == "stale_cursor"


@pytest.mark.parametrize("payload", [[], None, 5, {"scope": "wrong"}, {"scope": "wrong", "after": []}])
def test_validly_signed_malformed_cursor_payload_is_rejected(context, payload):
    row(context)
    with pytest.raises(reader.CanonicalReadError) as error:
        feed.read_feed(context.scope, selected(), cursor=signing.dumps(payload, salt=feed.SALT))
    assert error.value.code == "stale_cursor"


@pytest.mark.parametrize(
    "change", ["filter", "limit", "revision", "legacy_body", "source", "permissions", "new_row", "native"]
)
def test_cursor_is_bound_to_filters_source_permissions_identity_and_snapshot(context, settings, change):
    row(context)
    other = account(context)
    old = legacy(other)
    page = feed.read_feed(context.scope, selected(), limit=1)
    assert page["next_cursor"]
    filters, limit = selected(), 1
    if change == "filter":
        filters = selected(domain="comment")
    elif change == "limit":
        limit = 2
    elif change == "revision":
        InboxConversation.objects.filter(pk=context.conversation.pk).update(revision=F("revision") + 1)
    elif change == "legacy_body":
        InboxMessage.objects.filter(pk=old.pk).update(body="edited text")
    elif change == "source":
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    elif change == "permissions":
        context.member.delete()
    elif change == "new_row":
        legacy(other)
    else:
        SocialAccount.objects.filter(pk=other.pk).update(account_platform_id="replacement")
    with pytest.raises(reader.CanonicalReadError):
        feed.read_feed(context.scope, filters, cursor=page["next_cursor"], limit=limit)


@pytest.mark.parametrize("change", ["membership", "legacy_body", "canonical_revision", "source"])
def test_changes_during_projection_never_return_partially_authorized_page(context, settings, change):
    row(context)
    old = legacy(account(context))
    original = feed._project_legacy

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "membership":
            context.member.delete()
        elif change == "legacy_body":
            InboxMessage.objects.filter(pk=old.pk).update(body="replacement")
        elif change == "canonical_revision":
            InboxConversation.objects.filter(pk=context.conversation.pk).update(revision=F("revision") + 1)
        else:
            settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
        return result

    with patch.object(feed, "_project_legacy", side_effect=mutate), pytest.raises(reader.CanonicalReadError):
        feed.read_feed(context.scope, selected())


def test_api_key_allowlist_applies_to_both_sources_and_cursor(context):
    row(context)
    other = account(context)
    old = legacy(other)
    scope = reader.key_read_scope(context.key.api_key)
    result = feed.read_feed(scope, selected())
    assert [record["id"] for record in result["rows"]] == [str(context.conversation.pk)]
    assert str(old.pk) not in repr(result)
    with pytest.raises(reader.CanonicalReadError):
        feed.read_feed(scope, selected(account=str(other.pk)))
    context.key.api_key.social_accounts.add(other)
    page = feed.read_feed(scope, selected(), limit=1)
    context.key.api_key.social_accounts.remove(other)
    with pytest.raises(reader.CanonicalReadError):
        feed.read_feed(scope, selected(), limit=1, cursor=page["next_cursor"])


def test_historical_unsupported_and_disconnected_domains_remain_filterable(context):
    historic = account(context, platform="bluesky", connection_status="disconnected")
    saved = legacy(historic, kind="review")
    youtube = account(context, platform="youtube")
    sources = feed.read_feed(context.scope, selected())["sources"]
    review_context = feed.filter_context(context.scope, selected(domain="review"), sources)
    assert [source["id"] for source in review_context["account_sources"]] == [str(historic.pk)]
    assert [
        record["id"] for record in collect(context, filters=selected(domain="review", account=str(historic.pk)))
    ] == [str(saved.pk)]
    dm_context = feed.filter_context(context.scope, selected(domain="dm"), sources)
    assert str(youtube.pk) not in {source["id"] for source in dm_context["account_sources"]}
    assert str(historic.pk) not in {source["id"] for source in dm_context["account_sources"]}
    all_context = feed.filter_context(context.scope, selected(), sources)
    assert "review" in {tab["value"] for tab in all_context["domain_tabs"]}


def test_no_review_ingestion_is_invented_and_feed_is_read_only(context):
    row(context)
    old = legacy(account(context), kind="dm")
    counts = {
        model: model.objects.count()
        for model in (
            InboxMessage,
            InboxConversation,
            ConversationMessage,
            InboxReply,
            ConversationReadState,
            ConversationSyncIdentity,
            InboxSyncConnection,
        )
    }
    with (
        CaptureQueriesContext(connection) as queries,
        patch("apps.inbox.native_thread_reads.read_native_thread") as native,
    ):
        result = feed.read_feed(context.scope, selected())
        choices = feed.filter_context(context.scope, selected(), result["sources"])
        reviews = feed.read_feed(context.scope, selected(domain="review"))
    assert reviews["rows"] == []
    assert "review" not in {tab["value"] for tab in choices["domain_tabs"]}
    assert all("review" not in capabilities for capabilities in feed.CAPABILITIES.values())
    assert {model: model.objects.count() for model in counts} == counts
    assert not [
        query["sql"] for query in queries if query["sql"].lstrip().split()[0].upper() in {"UPDATE", "INSERT", "DELETE"}
    ]
    assert old.pk in {UUID(record["id"]) for record in result["rows"] if record["source"] == "legacy"}
    native.assert_not_called()


def test_session_route_defaults_to_all_types_and_htmx_retains_one_mixed_list(context, client):
    row(context)
    old = legacy(account(context), kind="dm")
    comment = legacy(context.account)
    client.force_login(context.user)
    url = reverse("inbox:feed", kwargs={"workspace_id": context.account.workspace_id})
    for headers in ({}, {"HTTP_HX_REQUEST": "true"}):
        response = client.get(url, **headers)
        assert response.status_code == 200
        assert response.context["active_domain"] == "all"
        assert {record["id"] for record in response.context["unified_rows"]} == {
            str(context.conversation.pk),
            str(old.pk),
            str(comment.pk),
        }
        assert response["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("change", ["membership", "account_owner", "canonical_source", "legacy_body"])
def test_session_render_rechecks_grants_sources_and_ownership_before_return(context, client, settings, change):
    from apps.inbox import unified_views

    row(context, body="PRIVATE CANONICAL BODY")
    other = account(context)
    message = legacy(other, body="PRIVATE LEGACY BODY")
    foreign = Workspace.objects.create(name="Foreign", organization=context.account.workspace.organization)
    client.force_login(context.user)
    original = unified_views.render

    def mutate(*args, **kwargs):
        response = original(*args, **kwargs)
        if change == "membership":
            context.member.delete()
        elif change == "account_owner":
            SocialAccount.objects.filter(pk=other.pk).update(workspace=foreign)
        elif change == "canonical_source":
            settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
        else:
            InboxMessage.objects.filter(pk=message.pk).update(body="CHANGED BODY")
        return response

    with patch.object(unified_views, "render", side_effect=mutate):
        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": context.account.workspace_id}))
    assert response.status_code in {404, 409}
    assert "PRIVATE" not in response.content.decode() and "CHANGED BODY" not in response.content.decode()


def test_expired_and_foreign_principal_cursors_cannot_replay(context):
    from apps.accounts.models import User
    from apps.members.models import WorkspaceMembership

    row(context)
    legacy(account(context))
    with patch("django.core.signing.time.time", return_value=1):
        old = feed.read_feed(context.scope, selected(), limit=1)["next_cursor"]
    with pytest.raises(reader.CanonicalReadError) as error:
        feed.read_feed(context.scope, selected(), cursor=old, limit=1)
    assert error.value.code == "stale_cursor"
    token = feed.read_feed(context.scope, selected(), limit=1)["next_cursor"]
    another = User.objects.create_user(email="other-reader@example.com", password="synthetic", name="Other reader")
    WorkspaceMembership.objects.create(user=another, workspace=context.account.workspace, workspace_role="owner")
    with pytest.raises(reader.CanonicalReadError):
        feed.read_feed(
            reader.session_read_scope(another, context.account.workspace_id), selected(), cursor=token, limit=1
        )


def test_unavailable_media_does_not_hide_valid_saved_text(context):
    message = row(context, body="Visible text despite unavailable media", content_status="unavailable")
    result = feed.read_feed(context.scope, selected())["rows"][0]
    assert result["canonical"]["latest_message"]["content_available"] is True
    assert result["preview"] == message.body


def test_legacy_shadow_uses_one_consistent_timestamp_for_global_order(context):
    other = account(context)
    now = timezone.now()
    thread = InboxConversation.objects.create(
        workspace=other.workspace,
        social_account=other,
        platform=other.platform,
        platform_conversation_id="native-legacy-shadow-thread",
        identity_kind="platform",
        peer_id="peer",
        conversation_type="direct",
        classification_reason="participants_pair",
    )
    message = row(
        SimpleNamespace(account=other, conversation=thread),
        direction="inbound",
        sender_id="peer",
        occurred_at=now,
    )
    shadow = legacy(other, kind="dm", platform_message_id=message.platform_message_id, stamp=now - timedelta(days=3))
    message.legacy_message = shadow
    message.save(update_fields=["legacy_message"])
    legacy(other, stamp=now - timedelta(days=1))
    records = collect(context)
    assert [record["timestamp"] for record in records] == sorted(
        (record["timestamp"] for record in records), reverse=True
    )
    assert all(record["stamp"].isoformat() == record["timestamp"] for record in records)


def test_signed_cursor_must_name_an_existing_exact_boundary(context):
    row(context)
    legacy(account(context))
    result = feed.read_feed(context.scope, selected(), limit=1)
    token = signing.loads(result["next_cursor"], salt=feed.SALT)
    token["after"][2] = str(uuid4())
    with pytest.raises(reader.CanonicalReadError) as error:
        feed.read_feed(context.scope, selected(), limit=1, cursor=signing.dumps(token, salt=feed.SALT))
    assert error.value.code == "stale_cursor"


@pytest.mark.parametrize("boundary", ["projection", "render"])
@pytest.mark.parametrize("change", ["withdrawal", "body", "message_identity"])
def test_unversioned_canonical_content_change_discards_the_response(context, client, boundary, change):
    from apps.inbox import unified_views

    message = row(context, body="PRIVATE BEFORE CONTENT CHANGE")
    proof(context, message)
    client.force_login(context.user)
    module, attribute = (feed, "_project_canonical") if boundary == "projection" else (unified_views, "render")
    original = getattr(module, attribute)

    def mutate(*args, **kwargs):
        response = original(*args, **kwargs)
        if change == "withdrawal":
            ConversationObservationState.objects.filter(message=message).update(withdrawn_at=timezone.now())
        elif change == "body":
            ConversationMessage.objects.filter(pk=message.pk).update(body="PRIVATE CHANGED CONTENT")
        else:
            ConversationMessage.objects.filter(pk=message.pk).update(platform_message_id="new-native-id")
        return response

    with patch.object(module, attribute, side_effect=mutate):
        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": context.account.workspace_id}))
    assert response.status_code == 409
    assert "PRIVATE" not in response.content.decode()


@pytest.mark.parametrize("change", ["message_platform", "conversation_account"])
def test_legacy_shadow_rebind_same_time_and_body_is_rejected_after_render(context, client, change):
    from apps.inbox import unified_views

    other = account(context)
    now = timezone.now()
    thread = InboxConversation.objects.create(
        workspace=other.workspace,
        social_account=other,
        platform=other.platform,
        platform_conversation_id="legacy-shadow",
        identity_kind="platform",
        peer_id="peer",
        conversation_type="direct",
        classification_reason="participants_pair",
    )
    message = row(
        SimpleNamespace(account=other, conversation=thread),
        direction="inbound",
        sender_id="peer",
        body="PRIVATE LEGACY SHADOW BODY",
        occurred_at=now,
    )
    shadow = legacy(other, kind="dm", platform_message_id=message.platform_message_id, body=message.body, stamp=now)
    message.legacy_message = shadow
    message.save(update_fields=["legacy_message"])
    client.force_login(context.user)
    original = unified_views.render

    def rebind(*args, **kwargs):
        response = original(*args, **kwargs)
        if change == "message_platform":
            ConversationMessage.objects.filter(pk=message.pk).update(platform="instagram_login")
        else:
            InboxConversation.objects.filter(pk=thread.pk).update(social_account=context.account)
        return response

    with patch.object(unified_views, "render", side_effect=rebind):
        response = client.get(reverse("inbox:feed", kwargs={"workspace_id": context.account.workspace_id}))
    assert response.status_code == 409
    assert "PRIVATE" not in response.content.decode()


def test_unsupported_empty_account_is_hidden_except_to_preserve_explicit_selection(context):
    empty = account(context, platform="bluesky")
    sources = feed.read_feed(context.scope, selected())["sources"]
    default = feed.filter_context(context.scope, selected(), sources)
    assert str(empty.pk) not in {source["id"] for source in default["account_sources"]}
    explicit = feed.filter_context(context.scope, selected(domain="dm", account=str(empty.pk)), sources)
    assert str(empty.pk) in {source["id"] for source in explicit["account_sources"]}
    assert feed.read_feed(context.scope, selected(domain="dm", account=str(empty.pk)))["rows"] == []


def test_overlong_search_and_ambiguous_legacy_type_alias_fail_closed():
    for parameters in ("q=" + "x" * 501, "type=comment&type=mention", "workflow=waiting&workflow=done"):
        with pytest.raises(reader.CanonicalReadError) as error:
            feed.filters(QueryDict(parameters))
        assert error.value.code == "invalid_filter"


@pytest.mark.parametrize("mode", ["status", "search"])
def test_cursor_rejects_public_parent_proof_change_outside_matching_filter(context, mode):
    now = timezone.now()
    root = legacy(
        context.account,
        platform_message_id="public-root",
        status="resolved",
        body="Parent evidence only",
        extra={"post_id": "page_post", "parent_id": ""},
    )
    first = legacy(
        context.account,
        stamp=now,
        status="unread",
        body="matching child",
        extra={"post_id": "page_post", "parent_id": "public-root"},
    )
    legacy(
        context.account,
        stamp=now - timedelta(minutes=1),
        status="unread",
        body="matching second child",
        extra={"post_id": "page_post", "parent_id": "public-root"},
    )
    legacy(context.account, stamp=now - timedelta(minutes=2), status="unread", body="matching unrelated thread")
    filters = (
        selected(domain="comment", status="unread") if mode == "status" else selected(domain="comment", q="matching")
    )
    initial = feed.read_feed(context.scope, filters, limit=1)
    assert [record["id"] for record in initial["rows"]] == [str(first.pk)]
    assert initial["rows"][0]["matched_count"] == 2 and initial["next_cursor"]
    InboxMessage.objects.filter(pk=root.pk).update(extra={"post_id": "differentpage_post", "parent_id": ""})
    with pytest.raises(reader.CanonicalReadError) as error:
        feed.read_feed(context.scope, filters, limit=1, cursor=initial["next_cursor"])
    assert error.value.code == "stale_cursor"


def test_selected_legacy_dm_retains_message_status_filter(context, client):
    other = account(context)
    unread = legacy(other, kind="dm", status="unread", extra={"conversation_id": "saved-legacy-thread"})
    resolved = legacy(other, kind="dm", status="resolved", extra={"conversation_id": "saved-legacy-thread"})
    client.force_login(context.user)
    url = reverse("inbox:feed", kwargs={"workspace_id": context.account.workspace_id})
    for status, expected in (("unread", unread), ("resolved", resolved)):
        filters = selected(domain="dm", account=str(other.pk), status=status)
        assert [record["id"] for record in collect(context, filters=filters)] == [str(expected.pk)]
        response = client.get(url, {"domain": "dm", "account": str(other.pk), "status": status})
        assert response.status_code == 200
        assert response.context["show_legacy_status"] is True
        assert [record["id"] for record in response.context["unified_rows"]] == [str(expected.pk)]


@pytest.mark.parametrize("scope_kind", ["canonical", "mixed", "held"])
def test_dm_status_filter_never_reinterprets_canonical_or_held_workflow(context, settings, scope_kind):
    message = row(context)
    legacy(account(context), kind="dm")
    if scope_kind == "held":
        proof(context, message)
        settings.INBOX_CONVERSATION_V2_READ_ACCOUNTS = []
    filters = selected(domain="dm", status="unread", account="" if scope_kind == "mixed" else str(context.account.pk))
    with pytest.raises(reader.CanonicalReadError) as error:
        feed.read_feed(context.scope, filters)
    assert error.value.code == "invalid_filter"


@pytest.mark.parametrize("domain", ["comment", "dm"])
def test_public_and_legacy_dm_queues_use_exact_assignee_and_reset_on_type_change(context, domain):
    from urllib.parse import parse_qs, urlsplit

    from apps.accounts.models import User

    other = account(context)
    another = User.objects.create_user(email="other-assignee@example.com", name="Another assignee")
    mine = legacy(other, kind=domain, assigned_to=context.user)
    unassigned = legacy(other, kind=domain)
    legacy(other, kind=domain, assigned_to=another)
    for view, expected in (("mine", mine), ("unassigned", unassigned)):
        filters = selected(domain=domain, account=str(other.pk), status="unread", view=view)
        result = feed.read_feed(context.scope, filters)
        assert [record["id"] for record in result["rows"]] == [str(expected.pk)]
        tabs = feed.filter_context(context.scope, filters, result["sources"])["domain_tabs"]
        for tab in tabs:
            parameters = parse_qs(urlsplit(tab["url"]).query)
            assert not {"view", "status", "workflow"} & parameters.keys()
            assert parameters["account"] == [str(other.pk)]


def test_queue_views_cannot_be_applied_to_canonical_or_mixed_dm_scope(context):
    row(context)
    legacy(account(context), kind="dm", assigned_to=context.user)
    for selected_account in ("", str(context.account.pk)):
        for view in ("mine", "unassigned"):
            with pytest.raises(reader.CanonicalReadError) as error:
                feed.read_feed(context.scope, selected(domain="dm", account=selected_account, view=view))
            assert error.value.code == "invalid_filter"
    with pytest.raises(reader.CanonicalReadError):
        selected(domain="dm", status="unread", workflow="waiting")
