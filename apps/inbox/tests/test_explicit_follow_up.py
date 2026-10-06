"""A deliberate follow-up is a new receipt-bound intent, never a retry."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.utils import timezone

from apps.inbox import reply_safety, services
from apps.inbox.dm_send_gate import DMSendGateError
from apps.inbox.models import DMSendAttempt, DMSendControl, InboxMessage, InboxReply
from apps.inbox.tests.test_shared_reply_safety import dm as dm  # noqa: F401
from apps.inbox.tests.test_shared_reply_safety import draft, send

pytestmark = pytest.mark.django_db(transaction=True)


def accept(mid):
    def provider(*args, **kwargs):
        kwargs["before_provider"]()
        return mid

    return provider


@pytest.fixture
def sent_parent(dm):
    parent = draft(dm)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accept("parent-outbound")):
        send(dm, parent)
    return parent


def follow_up(dm, parent, body="An explicit additional answer"):
    return services.create_reply_draft(message=dm.message, body=body, author=dm.user, follow_up_of=parent)


def test_explicit_follow_up_sends_one_additional_message_and_keeps_original_receipt(dm, sent_parent):
    original = InboxReply.objects.values().get(pk=sent_parent.pk)
    availability = services.reply_send_availability(dm.message, follow_up_of=sent_parent)
    assert availability["allowed"] is True
    child = follow_up(dm, sent_parent)
    assert child.is_follow_up and child.follow_up_of_id == sent_parent.pk
    assert not child.not_sent_verified and child.send_error == ""
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accept("follow-up-outbound")) as provider:
        send(dm, child)
    provider.assert_called_once()
    assert child.status == "sent" and child.platform_reply_id == "follow-up-outbound"
    assert not child.not_sent_verified
    assert InboxReply.objects.values().get(pk=sent_parent.pk) == original
    assert InboxReply.objects.count() == 2
    assert not DMSendControl.objects.exists() and not DMSendAttempt.objects.exists()


def test_normal_create_and_retry_never_implicitly_become_follow_up(dm, sent_parent):
    with pytest.raises(services.ReplyStateError, match="already has a sent"):
        draft(dm, "Different text is not explicit follow-up intent")
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(services.ReplyStateError):
        send(dm, sent_parent)
    provider.assert_not_called()
    assert InboxReply.objects.count() == 1


def test_double_click_reuses_child_and_changed_text_requires_explicit_edit(dm, sent_parent):
    child = follow_up(dm, sent_parent)
    assert follow_up(dm, sent_parent).pk == child.pk
    with pytest.raises(services.ReplyStateError, match="Open and edit"):
        follow_up(dm, sent_parent, "Another body")
    child.refresh_from_db()
    assert child.body == "An explicit additional answer"
    assert InboxReply.objects.count() == 2
    services.update_reply_draft(child, body="Reviewed additional answer")
    assert child.is_follow_up and child.follow_up_of_id == sent_parent.pk


def test_next_deliberate_follow_up_must_identify_the_new_sent_receipt(dm, sent_parent):
    child = follow_up(dm, sent_parent)
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accept("child-outbound")):
        send(dm, child)
    with pytest.raises(services.ReplyStateError, match="already has a follow-up"):
        follow_up(dm, sent_parent)
    assert services.reply_send_availability(dm.message, follow_up_of=sent_parent)["code"] == "follow_up_exists"
    grandchild = follow_up(dm, child, "One further deliberate answer")
    with patch("apps.inbox.services._dispatch_to_platform", side_effect=accept("grandchild-outbound")):
        send(dm, grandchild)
    assert grandchild.status == "sent" and grandchild.follow_up_of_id == child.pk
    assert InboxReply.objects.count() == 3


def test_uncertain_child_stays_held_and_cannot_spawn_another_intent(dm, sent_parent):
    child = follow_up(dm, sent_parent)

    def uncertain(*args, **kwargs):
        kwargs["before_provider"]()
        raise TimeoutError()

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=uncertain),
        pytest.raises(DMSendGateError, match="unknown"),
    ):
        send(dm, child)
    child.refresh_from_db()
    assert child.status == "unknown" and not child.not_sent_verified
    with pytest.raises(services.ReplyStateError):
        follow_up(dm, sent_parent)
    with patch("apps.inbox.services._dispatch_to_platform") as provider, pytest.raises(services.ReplyStateError):
        send(dm, child)
    provider.assert_not_called()
    assert InboxReply.objects.count() == 2


@pytest.mark.parametrize(
    "mutation", ["draft", "failed", "unknown", "missing_receipt", "missing_time", "future_time", "stale_receipt"]
)
def test_parent_must_still_be_a_valid_sent_provider_receipt(dm, sent_parent, mutation):
    changes = {}
    if mutation in {"draft", "failed", "unknown"}:
        changes["status"] = mutation
    elif mutation == "missing_receipt":
        changes["platform_reply_id"] = ""
    elif mutation == "missing_time":
        changes["sent_at"] = None
    elif mutation == "future_time":
        changes["sent_at"] = timezone.now() + timedelta(days=1)
    else:
        changes["platform_reply_id"] = "unexpected-replacement-receipt"
    InboxReply.objects.filter(pk=sent_parent.pk).update(**changes)
    with pytest.raises(services.ReplyStateError, match="selected sent reply"):
        follow_up(dm, sent_parent)
    assert InboxReply.objects.count() == 1


def test_parent_from_another_incoming_message_cannot_authorize_a_follow_up(dm, sent_parent):
    original = dm.message
    other = InboxMessage.objects.get(pk=original.pk)
    other.pk = None
    other.platform_message_id = "another-incoming"
    other.save()
    with pytest.raises(services.ReplyStateError, match="selected sent reply"):
        services.create_reply_draft(message=other, body="Misbound", author=dm.user, follow_up_of=sent_parent)
    result = services.reply_send_availability(other, follow_up_of=sent_parent)
    assert result["code"] == "follow_up_parent_invalid" and result["existing_reply_id"] is None
    assert InboxReply.objects.count() == 1


def test_removed_parent_leaves_pending_child_explicitly_held(dm, sent_parent):
    child = follow_up(dm, sent_parent)
    sent_parent.delete()
    child.refresh_from_db()
    assert child.follow_up_of_id is None and child.is_follow_up
    assert services.reply_send_availability(dm.message, reply=child)["code"] == "follow_up_parent_missing"
    with (
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(services.ReplyStateError, match="no longer available"),
    ):
        send(dm, child)
    provider.assert_not_called()
    assert InboxReply.objects.filter(pk=child.pk).exists()


def test_stale_child_read_after_parent_deletion_returns_a_hold(dm, sent_parent):
    child = follow_up(dm, sent_parent)
    stale_child = InboxReply.objects.get(pk=child.pk)
    sent_parent.delete()
    assert services.reply_send_availability(dm.message, reply=stale_child)["code"] == "follow_up_parent_missing"


def test_parent_removed_after_marker_never_releases_follow_up_bypass(dm, sent_parent):
    child = follow_up(dm, sent_parent)
    original = reply_safety._prepare_receipt

    def remove_parent(*args, **kwargs):
        generation = original(*args, **kwargs)
        sent_parent.delete()
        return generation

    with (
        patch.object(reply_safety, "_prepare_receipt", side_effect=remove_parent),
        patch("apps.inbox.services._dispatch_to_platform") as provider,
        pytest.raises(DMSendGateError, match="no longer available"),
    ):
        send(dm, child)
    provider.assert_not_called()
    child.refresh_from_db()
    assert child.status == "failed" and child.not_sent_verified
    assert child.is_follow_up and child.follow_up_of_id is None
    with (
        patch("apps.inbox.services._dispatch_to_platform") as retry,
        pytest.raises(DMSendGateError, match="no longer available"),
    ):
        send(dm, child)
    retry.assert_not_called()


@pytest.mark.parametrize("change", ["permission", "window", "parent"])
def test_follow_up_rechecks_authority_window_and_parent_before_http(dm, sent_parent, change):
    child = follow_up(dm, sent_parent)

    def change_before_post(*args, **kwargs):
        if change == "permission":
            dm.member.delete()
        elif change == "window":
            # Simulate time spent resolving credentials, not a new inbound.
            with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(days=2)):
                kwargs["before_provider"]()
        else:
            InboxReply.objects.filter(pk=sent_parent.pk).update(platform_reply_id="changed-receipt")
        kwargs["before_provider"]()
        pytest.fail("Provider POST entered after preflight invalidation")

    with (
        patch("apps.inbox.services._dispatch_to_platform", side_effect=change_before_post),
        pytest.raises(services.ReplyStateError),
    ):
        send(dm, child)
    child.refresh_from_db()
    assert child.status == "failed" and child.not_sent_verified


def test_definitive_failure_metadata_is_not_inferred_from_error_text(dm):
    reply = draft(dm)
    reply.status = "failed"
    reply.send_error = "Confirmed not sent: copied untrusted text"
    reply.save(update_fields=["status", "send_error"])
    assert reply_safety.is_unresolved_reply(reply)


def test_database_uniqueness_prevents_two_children_for_one_receipt(dm, sent_parent):
    follow_up(dm, sent_parent)
    with transaction.atomic(), pytest.raises(IntegrityError):
        InboxReply.objects.create(
            inbox_message=dm.message, body="Duplicate child", follow_up_of=sent_parent, is_follow_up=True
        )
    assert InboxReply.objects.filter(follow_up_of=sent_parent).count() == 1


def test_postgres_competing_follow_up_intents_share_one_child(dm, sent_parent):
    if connection.vendor != "postgresql":
        pytest.skip("Requires actual PostgreSQL row locks and committed connections")
    at_lock = Barrier(2)
    original_lock = services.lock_dm_account

    def lock(*args, **kwargs):
        at_lock.wait(timeout=10)
        return original_lock(*args, **kwargs)

    def create():
        close_old_connections()
        try:
            return follow_up(dm, sent_parent).pk
        finally:
            close_old_connections()

    with patch.object(services, "lock_dm_account", side_effect=lock), ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(create) for _ in range(2)]
        assert len({result.result(timeout=15) for result in results}) == 1
    assert InboxReply.objects.filter(follow_up_of=sent_parent).count() == 1
