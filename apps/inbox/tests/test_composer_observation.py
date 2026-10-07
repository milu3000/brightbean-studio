"""Observing a newest page proves only its exact scoped revision, never a send grant."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox import canonical_reads as reader
from apps.inbox.models import ConversationReadState, InboxConversation, InboxReply
from apps.inbox.tests.test_canonical_reads_rebuilt import context as _context
from apps.inbox.tests.test_canonical_reads_rebuilt import row

context = _context
pytestmark = pytest.mark.django_db


def test_outgoing_only_newest_page_supplies_observation_without_read_or_send_mutation(context):
    row(context)
    page = reader.read_conversation(context.scope, context.conversation.pk)
    assert page["read_ack_token"] is None
    token = page["composer_observation_token"]
    assert token
    verified = reader.verify_composer_observation(context.scope, context.conversation.pk, token)
    assert verified["conversation_id"] == str(context.conversation.pk)
    assert verified["incoming_generation"] == 0
    assert not ConversationReadState.objects.exists() and not InboxReply.objects.exists()


def test_older_and_undated_continuations_cannot_mint_latest_observation(context):
    row(context, occurred_at=timezone.now() - timedelta(days=1))
    row(context)
    for _ in range(12):
        row(context, occurred_at=None)
    page = reader.read_conversation(context.scope, context.conversation.pk, limit=1)
    for cursor in [page["next_cursor"], page["undated_next_cursor"]]:
        assert cursor
        older = reader.read_conversation(context.scope, context.conversation.pk, limit=1, cursor=cursor)
        assert older["composer_observation_token"] is None


@pytest.mark.parametrize("change", ["revision", "generation", "peer", "grants"])
def test_observation_requires_exact_current_snapshot(context, change):
    row(context)
    token = reader.read_conversation(context.scope, context.conversation.pk)["composer_observation_token"]
    if change == "grants":
        context.member.delete()
    else:
        context.conversation.refresh_from_db()
        update = {
            "revision": {"revision": context.conversation.revision + 1},
            "generation": {"incoming_generation": 1},
            "peer": {"peer_id": "new-peer"},
        }[change]
        InboxConversation.objects.filter(pk=context.conversation.pk).update(**update)
    with pytest.raises(reader.CanonicalReadError):
        reader.verify_composer_observation(context.scope, context.conversation.pk, token)
    assert not ConversationReadState.objects.exists()


def test_observation_cannot_cross_actor_principal_or_conversation(context):
    row(context)
    token = reader.read_conversation(context.scope, context.conversation.pk)["composer_observation_token"]
    with pytest.raises(reader.CanonicalReadError):
        reader.verify_composer_observation(reader.key_read_scope(context.key.api_key), context.conversation.pk, token)
    from uuid import uuid4

    with pytest.raises(reader.CanonicalReadError):
        reader.verify_composer_observation(context.scope, uuid4(), token)


@pytest.fixture
def owned(context):
    from apps.inbox.models import DMConversationOwnership, DMSendControl

    control = DMSendControl.objects.create(
        social_account=context.account,
        workspace=context.account.workspace,
        platform=context.account.platform,
        account_platform_id=context.account.account_platform_id,
        paused=False,
        epoch=3,
        coverage_from=timezone.now() - timedelta(days=1),
        coverage_version="synthetic-v1",
    )
    owner = DMConversationOwnership.objects.create(
        control=control,
        conversation=context.conversation,
        workspace=context.account.workspace,
        social_account=context.account,
        platform=context.account.platform,
        account_platform_id=context.account.account_platform_id,
        platform_conversation_id=context.conversation.platform_conversation_id,
        peer_id=context.conversation.peer_id,
        identity_kind=context.conversation.identity_kind,
        owner_scope="human:synthetic",
        paused=False,
        epoch=7,
    )
    return owner, control


def test_observation_reports_existing_owner_and_control_fences_without_mutation(context, owned):
    owner, control = owned
    row(context)
    before = (owner.updated_at, control.updated_at)
    token = reader.read_conversation(context.scope, context.conversation.pk)["composer_observation_token"]
    proof = reader.verify_composer_observation(context.scope, context.conversation.pk, token)["owner_proof"]
    assert (proof["owner_id"], proof["owner_epoch"], proof["control_id"], proof["control_epoch"]) == (
        str(owner.pk),
        7,
        str(control.pk),
        3,
    )
    owner.refresh_from_db()
    control.refresh_from_db()
    assert (owner.updated_at, control.updated_at) == before


@pytest.mark.parametrize(
    "change",
    ["owner_epoch", "control_epoch", "owner_pause", "control_pause", "owner_cutoff", "control_cutoff", "owner_scope"],
)
def test_pre_resume_observation_cannot_cross_dispatch_fence_change(context, owned, change):
    owner, control = owned
    row(context)
    token = reader.read_conversation(context.scope, context.conversation.pk)["composer_observation_token"]
    context.conversation.refresh_from_db()
    revision = context.conversation.revision
    model, target, values = {
        "owner_epoch": (type(owner), owner.pk, {"epoch": 8}),
        "control_epoch": (type(control), control.pk, {"epoch": 4}),
        "owner_pause": (type(owner), owner.pk, {"paused": True}),
        "control_pause": (type(control), control.pk, {"paused": True}),
        "owner_cutoff": (type(owner), owner.pk, {"resume_cutoff": timezone.now()}),
        "control_cutoff": (type(control), control.pk, {"resume_cutoff": timezone.now()}),
        "owner_scope": (type(owner), owner.pk, {"owner_scope": "different-human"}),
    }[change]
    model.objects.filter(pk=target).update(**values)
    context.conversation.refresh_from_db()
    assert context.conversation.revision == revision
    with pytest.raises(reader.CanonicalReadError, match="displayed conversation changed"):
        reader.verify_composer_observation(context.scope, context.conversation.pk, token)
    fresh = reader.read_conversation(context.scope, context.conversation.pk)["composer_observation_token"]
    assert reader.verify_composer_observation(context.scope, context.conversation.pk, fresh)["owner_proof"]


def test_pre_enrollment_observation_does_not_adopt_new_control(context):
    from apps.inbox.models import DMSendControl

    row(context)
    token = reader.read_conversation(context.scope, context.conversation.pk)["composer_observation_token"]
    DMSendControl.objects.create(
        social_account=context.account,
        workspace=context.account.workspace,
        platform=context.account.platform,
        account_platform_id=context.account.account_platform_id,
        coverage_from=timezone.now(),
        coverage_version="synthetic",
    )
    with pytest.raises(reader.CanonicalReadError):
        reader.verify_composer_observation(context.scope, context.conversation.pk, token)


def test_dispatch_fence_change_during_verification_is_rechecked(context, owned):
    from unittest.mock import patch

    from apps.inbox import composer_observation

    owner, _control = owned
    row(context)
    token = reader.read_conversation(context.scope, context.conversation.pk)["composer_observation_token"]
    snapshot = composer_observation._owner_proof
    calls = 0

    def change_after_first(conversation):
        nonlocal calls
        value = snapshot(conversation)
        calls += 1
        if calls == 1:
            type(owner).objects.filter(pk=owner.pk).update(epoch=8)
        return value

    with (
        patch.object(composer_observation, "_owner_proof", side_effect=change_after_first),
        pytest.raises(reader.CanonicalReadError),
    ):
        reader.verify_composer_observation(context.scope, context.conversation.pk, token)


def test_corrupt_owner_scope_never_becomes_valid_observation(context, owned):
    owner, _control = owned
    type(owner).objects.filter(pk=owner.pk).update(account_platform_id="other-native")
    row(context)
    token = reader.read_conversation(context.scope, context.conversation.pk)["composer_observation_token"]
    with pytest.raises(reader.CanonicalReadError):
        reader.verify_composer_observation(context.scope, context.conversation.pk, token)
