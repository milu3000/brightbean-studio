"""Synthetic evidence for distinct public threads and monotonic mention facets."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.inbox.models import InboxMessage
from apps.inbox.presentation import inbox_page, stored_thread_messages
from apps.inbox.public_threads import public_thread_key, public_type_filter
from apps.inbox.tasks import InboxSyncEngine
from apps.inbox.webhooks import _create_if_new, _facebook_comment_extra, _upsert_facebook_comment

pytestmark = pytest.mark.django_db


def row(account, mid, *, kind="comment", body="A public comment", **extra):
    return InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id=mid,
        message_type=kind,
        sender_name="Synthetic contact",
        body=body,
        received_at=timezone.now(),
        extra=extra,
    )


def ingest(account, source, *, mid="comment-1", kind="comment", body="Actual comment", extra=None):
    extra = extra or {"post_id": "page_post", "parent_id": ""}
    with patch.object(InboxSyncEngine, "_notify_new_message"):
        if source == "webhook":
            _create_if_new(account, mid, kind, "Contact", "peer", body, extra)
        else:
            InboxSyncEngine()._upsert_message(
                account,
                SimpleNamespace(
                    platform_message_id=mid,
                    message_type=kind,
                    sender_name="Contact",
                    sender_id="peer",
                    text=body,
                    timestamp=timezone.now(),
                    extra=extra,
                ),
            )


@pytest.mark.parametrize("first", ["poll", "webhook"])
@pytest.mark.parametrize("mention_first", [True, False])
def test_comment_and_mention_are_one_event_with_both_facets(inbox_account, first, mention_first):
    later = "webhook" if first == "poll" else "poll"
    pointer = "You were mentioned on Instagram. Open the post to read the mention."
    order = [("mention", pointer), ("comment", "Actual comment")]
    if not mention_first:
        order.reverse()
    for source, (kind, body) in zip((first, later), order, strict=True):
        ingest(inbox_account, source, kind=kind, body=body, extra={"post_id": "post", "reply_edge": "comment"})
    saved = InboxMessage.objects.get()
    assert saved.body == "Actual comment"
    assert saved.message_type == "comment" and saved.extra["is_mention"] is True
    assert InboxMessage.objects.filter(public_type_filter("mention")).get() == saved
    assert InboxMessage.objects.filter(public_type_filter("comment")).get() == saved


def test_media_mention_is_not_a_comment(inbox_account):
    mention = row(inbox_account, "media", kind="mention", reply_edge="media")
    assert not InboxMessage.objects.filter(public_type_filter("comment")).exists()
    assert InboxMessage.objects.filter(public_type_filter("mention")).get() == mention


def test_proved_root_and_children_share_one_public_thread(inbox_account):
    root = row(inbox_account, "root", post_id="page_post", parent_id="")
    child = row(inbox_account, "child", post_id="page_post", parent_id="root", is_mention=True)
    sibling = row(inbox_account, "second-root", post_id="page_post", parent_id="")
    assert public_thread_key(root) == public_thread_key(child)
    assert public_thread_key(root) != public_thread_key(sibling)
    assert set(stored_thread_messages(child)) == {root, child}
    assert len(inbox_page(InboxMessage.objects.all(), 1)) == 2


@pytest.mark.parametrize("change", ["missing_parent", "missing_post", "cycle", "different_post", "untrusted_root"])
def test_incomplete_or_conflicting_evidence_never_guesses_a_thread(inbox_account, change):
    root = row(inbox_account, "root", post_id="page_post", parent_id="")
    extra = {"post_id": "page_post", "parent_id": "root"}
    if change == "missing_parent":
        extra["parent_id"] = "unseen"
    elif change == "missing_post":
        extra.pop("post_id")
    elif change == "different_post":
        extra["post_id"] = "different_page_post"
    elif change == "untrusted_root":
        extra = {"post_id": "page_post", "root_comment_id": "root", "thread_id": "root"}
    elif change == "cycle":
        root.extra["parent_id"] = "child"
        root.save(update_fields=["extra"])
    child = row(inbox_account, "child", **extra)
    assert public_thread_key(root) != public_thread_key(child)
    assert list(stored_thread_messages(child)) == [child]


def test_facebook_root_normalization_preserves_proof_without_changing_reply_target():
    from providers.facebook import FacebookProvider

    extra = _facebook_comment_extra({"post_id": "page_post", "parent_id": "page_post"})
    assert extra["parent_id"] == ""
    assert FacebookProvider._comment_reply_target("root", extra) == "root"
    ambiguous = _facebook_comment_extra({"post_id": "page_post", "parent_id": "post"})
    assert "parent_id" not in ambiguous
    assert FacebookProvider._comment_reply_target("root", ambiguous) == "root"


@pytest.mark.parametrize("source", ["poll", "webhook"])
def test_public_event_cannot_replace_a_private_message(inbox_account, source):
    original = row(inbox_account, "collision", kind="dm", body="Private original")
    ingest(inbox_account, source, mid="collision")
    original.refresh_from_db()
    assert original.message_type == "dm" and original.body == "Private original"


@pytest.mark.parametrize("source", ["poll", "webhook"])
def test_private_event_cannot_replace_a_public_message(inbox_account, source):
    original = row(inbox_account, "collision", body="Public original")
    ingest(inbox_account, source, mid="collision", kind="dm")
    original.refresh_from_db()
    assert original.message_type == "comment" and original.body == "Public original"


@pytest.mark.parametrize("verb", ["add", "edit", "remove", "hide", "block", "mute"])
def test_stale_account_cannot_mutate_public_events_after_disconnect(inbox_account, verb):
    original = row(inbox_account, "comment", body="Keep history")
    type(inbox_account).objects.filter(pk=inbox_account.pk).update(connection_status="disconnected")
    _upsert_facebook_comment(inbox_account, {"comment_id": "comment", "verb": verb, "message": "Changed"})
    original.refresh_from_db()
    assert original.body == "Keep history" and original.status == "unread"


@pytest.mark.parametrize("source", ["poll", "webhook", "feed"])
def test_old_native_identity_cannot_write_after_same_platform_rebind(inbox_account, source):
    original = row(inbox_account, "comment-1", body="Keep history")
    type(inbox_account).objects.filter(pk=inbox_account.pk).update(account_platform_id="replacement-account")
    if source == "feed":
        _upsert_facebook_comment(inbox_account, {"comment_id": "comment-1", "verb": "edit", "message": "Changed"})
    else:
        ingest(inbox_account, source)
    original.refresh_from_db()
    assert original.body == "Keep history"


def test_proved_threads_do_not_cross_accounts_or_full_post_ids(inbox_account):
    other = type(inbox_account).objects.create(
        workspace=inbox_account.workspace, platform=inbox_account.platform, account_platform_id="other-account"
    )
    root = row(inbox_account, "root", post_id="page_post", parent_id="")
    child = row(other, "child", post_id="page_post", parent_id="root")
    assert public_thread_key(root) != public_thread_key(child)
    assert list(stored_thread_messages(child)) == [child]


@pytest.mark.parametrize("source", ["poll", "webhook", "remove"])
@pytest.mark.parametrize("kind", ["comment", "dm"])
def test_reassigned_account_cannot_move_or_mutate_another_workspaces_history(inbox_account, source, kind):
    from apps.workspaces.models import Workspace

    original = row(inbox_account, "comment-1", kind=kind, body="Original workspace history")
    previous_workspace_id = original.workspace_id
    other = Workspace.objects.create(
        name="Other synthetic workspace", organization=inbox_account.workspace.organization
    )
    type(inbox_account).objects.filter(pk=inbox_account.pk).update(workspace=other)
    inbox_account.refresh_from_db()
    if source == "remove":
        _upsert_facebook_comment(inbox_account, {"comment_id": "comment-1", "verb": "remove"})
    else:
        ingest(inbox_account, source, kind=kind)
    original.refresh_from_db()
    assert original.workspace_id == previous_workspace_id
    assert original.body == "Original workspace history" and original.status == "unread"


def test_public_notification_facet_dedup_and_proved_thread_grouping(user, inbox_account):
    from apps.members.models import WorkspaceMembership
    from apps.notifications.inbox import notify_legacy_incoming
    from apps.notifications.models import InboxNotificationEvent, Notification

    WorkspaceMembership.objects.update_or_create(
        user=user, workspace=inbox_account.workspace, defaults={"workspace_role": "owner"}
    )
    root = row(inbox_account, "root", post_id="post", parent_id="")
    data = {"message_id": str(root.pk), "workspace_id": str(root.workspace_id)}
    first = notify_legacy_incoming(user, data)
    assert first is not None
    root.message_type = "mention"
    root.extra["reply_edge"] = "comment"
    root.save(update_fields=["message_type", "extra"])
    assert notify_legacy_incoming(user, data).pk == first.pk
    child = row(inbox_account, "child", post_id="post", parent_id="root")
    assert (
        notify_legacy_incoming(user, {"message_id": str(child.pk), "workspace_id": str(child.workspace_id)}).pk
        == first.pk
    )
    assert Notification.objects.count() == 1 and InboxNotificationEvent.objects.count() == 2
    first.refresh_from_db()
    assert first.revision == 2


@pytest.mark.parametrize("source", ["poll", "webhook", "feed"])
@pytest.mark.parametrize("invalid", [" comment-1 ", "comment-1\n", 123, None])
def test_malformed_public_id_never_normalizes_into_existing_identity(inbox_account, source, invalid):
    original = row(inbox_account, "comment-1", body="Keep exact identity")
    if source == "feed":
        _upsert_facebook_comment(inbox_account, {"comment_id": invalid, "verb": "edit", "message": "Changed"})
    else:
        ingest(inbox_account, source, mid=invalid)
    original.refresh_from_db()
    assert original.body == "Keep exact identity" and InboxMessage.objects.count() == 1


@pytest.mark.parametrize("marker", ["false", "true", 1, [], {}])
def test_public_enrichment_does_not_promote_malformed_mention_marker(inbox_account, marker):
    original = row(inbox_account, "comment-1", is_mention=marker)
    assert not InboxMessage.objects.filter(public_type_filter("mention")).exists()
    ingest(inbox_account, "poll")
    original.refresh_from_db()
    assert original.extra.get("is_mention") is not True
    assert not InboxMessage.objects.filter(public_type_filter("mention")).exists()


@pytest.mark.parametrize("source", ["poll", "webhook"])
def test_related_post_never_links_another_workspace_after_account_move(inbox_account, source):
    from apps.composer.models import PlatformPost, Post
    from apps.inbox.tasks import resolve_related_posts
    from apps.workspaces.models import Workspace

    old = PlatformPost.objects.create(
        post=Post.objects.create(workspace=inbox_account.workspace, caption="Other workspace post"),
        social_account=inbox_account,
        platform_post_id="post",
    )
    other = Workspace.objects.create(name="Other workspace", organization=inbox_account.workspace.organization)
    type(inbox_account).objects.filter(pk=inbox_account.pk).update(workspace=other)
    inbox_account.refresh_from_db()
    extra = {"post_id": "page_post", "stored_post_id": "post", "parent_id": ""}
    assert resolve_related_posts(inbox_account, [SimpleNamespace(extra=extra)]) == {}
    if source == "poll":
        with patch.object(InboxSyncEngine, "_notify_new_message"):
            InboxSyncEngine()._upsert_message(
                inbox_account,
                SimpleNamespace(
                    platform_message_id="new",
                    message_type="comment",
                    sender_name="Contact",
                    sender_id="peer",
                    text="New comment",
                    timestamp=timezone.now(),
                    extra=extra,
                ),
                related_post_id=old.pk,
            )
    else:
        ingest(inbox_account, source, mid="new", extra=extra)
    assert InboxMessage.objects.get(platform_message_id="new").related_post_id is None
