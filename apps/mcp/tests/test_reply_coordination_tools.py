"""Coordination reads are permission scoped and never authorize or mutate sends."""

import json
import uuid
from copy import deepcopy

import pytest
from django.utils import timezone

from apps.api_keys.services import issue_api_key
from apps.inbox.models import ConversationMessage, ConversationWorkState, InboxConversation, SendOperation
from apps.mcp.tests.test_conversation_tools import call, observation
from apps.mcp.tests.test_conversation_tools import conversation as _conversation
from apps.mcp.tests.test_inbox_tools import _call, _SecureClient
from apps.mcp.tests.test_inbox_tools import account as _account
from apps.mcp.tests.test_inbox_tools import full_client as _full_client
from apps.mcp.tests.test_inbox_tools import memberships as _memberships
from apps.mcp.tests.test_inbox_tools import organization as _organization
from apps.mcp.tests.test_inbox_tools import other_account as _other_account
from apps.mcp.tests.test_inbox_tools import user as _user
from apps.mcp.tests.test_inbox_tools import workspace as _workspace
from apps.mcp.tools import all_tools, get_tool

pytestmark = pytest.mark.django_db
conversation = _conversation
account = _account
full_client = _full_client
memberships = _memberships
organization = _organization
other_account = _other_account
user = _user
workspace = _workspace


@pytest.fixture(autouse=True)
def flags(settings):
    settings.INBOX_CONVERSATION_V2_ENABLED = True
    settings.INBOX_REPLY_COORDINATION_ENABLED = True


def read(client, conversation):
    return call(client, "get_reply_coordination", {"conversation_id": str(conversation.pk)})


@pytest.fixture
def state(conversation):
    latest = observation(conversation, "coordination-target", sender_id="synthetic-peer")
    return ConversationWorkState.objects.create(
        conversation=conversation,
        generation=3,
        conversation_revision=conversation.revision,
        latest_incoming=latest,
        burst_started_at=timezone.now(),
        latest_incoming_at=timezone.now(),
        due_at=timezone.now(),
        owner_paused=True,
    )


@pytest.fixture
def operation(conversation, state):
    row = SendOperation.objects.create(
        workspace=conversation.workspace,
        social_account=conversation.social_account,
        platform=conversation.platform,
        conversation=conversation,
        actor_scope="private-actor",
        idempotency_key="private-idempotency",
        payload_fingerprint="private-fingerprint",
        body="Private draft text",
        target=state.latest_incoming,
        expected_revision=conversation.revision,
        expected_generation=state.generation,
        status="outcome_unknown",
        claim_token=uuid.uuid4(),
        fencing_token=9,
    )
    state.active_operation = row
    state.save(update_fields=["active_operation"])
    return row


@pytest.mark.parametrize("history,coordination", [(False, False), (False, True), (True, False)])
def test_both_flags_required_for_catalog_and_cached_call(settings, full_client, conversation, history, coordination):
    settings.INBOX_CONVERSATION_V2_ENABLED = history
    settings.INBOX_REPLY_COORDINATION_ENABLED = coordination
    assert get_tool("get_reply_coordination") is None
    assert "get_reply_coordination" not in {tool.name for tool in all_tools()}
    _, result = _call(full_client, "get_reply_coordination", {"conversation_id": str(conversation.pk)})
    assert "error" in result


def test_old_tool_wire_schemas_unchanged_by_coordination_flag(settings):
    enabled = {tool.name: deepcopy(tool.to_mcp_dict()) for tool in all_tools() if tool.name != "get_reply_coordination"}
    settings.INBOX_REPLY_COORDINATION_ENABLED = False
    assert {tool.name: tool.to_mcp_dict() for tool in all_tools()} == enabled


def test_read_without_state_never_creates_work(full_client, conversation):
    result = read(full_client, conversation)
    assert result["coordination_status"] == "not_started"
    assert result["work"] is None
    assert result["active_operation"] is None
    assert result["provider_dispatch_enabled"] is False
    assert result["send_preconditions_enforced"] is False
    assert result["freshness_complete"] is False
    assert not ConversationWorkState.objects.exists()
    assert not SendOperation.objects.exists()


def test_read_is_safe_bounded_projection_without_secrets_or_writes(full_client, conversation, state, operation):
    before_state = list(ConversationWorkState.objects.values())
    before_operations = list(SendOperation.objects.values())
    result = read(full_client, conversation)
    assert result["coordination_status"] == "observed"
    assert result["work"]["owner_paused"] is True
    assert result["work"]["generation"] == 3
    assert result["work"]["latest_incoming_id"] == str(state.latest_incoming_id)
    assert result["active_operation"]["status"] == "outcome_unknown"
    assert result["local_preflight_evaluated"] is False
    serialized = json.dumps(result)
    for secret in (
        "private-actor",
        "private-idempotency",
        "private-fingerprint",
        "Private draft text",
        str(operation.claim_token),
    ):
        assert secret not in serialized
    assert len(serialized) < 8192
    assert list(ConversationWorkState.objects.values()) == before_state
    assert list(SendOperation.objects.values()) == before_operations


def test_stale_revision_is_reported_without_claim(full_client, conversation, state):
    conversation.revision += 1
    conversation.save(update_fields=["revision"])
    assert read(full_client, conversation)["work"]["revision_matches"] is False
    assert not SendOperation.objects.exists()


def test_identity_quarantine_is_visible_and_cannot_be_cleared_by_read(full_client, conversation, state):
    state.identity_quarantined = True
    state.save(update_fields=["identity_quarantined"])
    assert read(full_client, conversation)["work"]["identity_quarantined"] is True
    state.refresh_from_db()
    assert state.identity_quarantined is True


def test_unknown_target_order_is_visible_without_inferring_send_eligibility(full_client, conversation, state):
    state.ordering_uncertain = True
    state.save(update_fields=["ordering_uncertain"])
    result = read(full_client, conversation)
    assert result["work"]["ordering_uncertain"] is True
    assert result["local_preflight_evaluated"] is False
    assert result["provider_dispatch_enabled"] is False


def test_history_gap_is_visible_without_mutation(full_client, conversation, state):
    state.history_gap = True
    state.save(update_fields=["history_gap"])
    result = read(full_client, conversation)
    assert result["work"]["history_gap"] is True
    state.refresh_from_db()
    assert state.history_gap is True


def test_no_inbox_permission_denies_read(user, memberships, workspace, account, conversation):
    key = issue_api_key(workspace=workspace, social_accounts=[account], issued_by=user, name="no-read", permissions=[])
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
    _, result = _call(client, "get_reply_coordination", {"conversation_id": str(conversation.pk)})
    assert "Permission denied" in result["error"]["message"]


def foreign_conversation(other_account):
    return InboxConversation.objects.create(
        workspace=other_account.workspace,
        social_account=other_account,
        platform=other_account.platform,
        platform_conversation_id="foreign-conversation",
        peer_id="foreign-peer",
        identity_kind="platform",
    )


def test_missing_and_unallowlisted_conversation_have_same_error(full_client, other_account):
    foreign = foreign_conversation(other_account)
    _, missing = _call(full_client, "get_reply_coordination", {"conversation_id": str(uuid.uuid4())})
    _, forbidden = _call(full_client, "get_reply_coordination", {"conversation_id": str(foreign.pk)})
    assert missing["error"] == forbidden["error"]


@pytest.mark.parametrize("change", ["workspace", "platform"])
def test_current_account_scope_changes_fail_closed(full_client, conversation, account, change):
    if change == "platform":
        account.platform = "threads"
        account.save(update_fields=["platform"])
    else:
        from apps.workspaces.models import Workspace

        moved = Workspace.objects.create(name="Moved workspace", organization=account.workspace.organization)
        account.workspace = moved
        account.save(update_fields=["workspace"])
    _, result = _call(full_client, "get_reply_coordination", {"conversation_id": str(conversation.pk)})
    assert "error" in result


@pytest.mark.parametrize("allow_both", [False, True])
def test_foreign_latest_fk_is_hidden_even_when_both_accounts_allowed(
    full_client, user, memberships, workspace, conversation, account, other_account, state, allow_both
):
    foreign = foreign_conversation(other_account)
    leaked = observation(foreign, "foreign-target", body="Foreign private context")
    state.latest_incoming = leaked
    state.save(update_fields=["latest_incoming"])
    client = full_client
    if allow_both:
        key = issue_api_key(
            workspace=workspace,
            social_accounts=[account, other_account],
            issued_by=user,
            name="both",
            permissions=["use_inbox"],
        )
        client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
    result = read(client, conversation)
    assert result["coordination_status"] == "unavailable"
    assert result["work"] is None
    assert str(leaked.pk) not in json.dumps(result)


def test_operation_cross_account_relationship_does_not_leak(full_client, conversation, other_account, state, operation):
    operation.social_account = other_account
    operation.save(update_fields=["social_account"])
    result = read(full_client, conversation)
    assert result["coordination_status"] == "unavailable"
    assert str(operation.pk) not in json.dumps(result)


def test_operation_target_cross_conversation_is_hidden(full_client, conversation, other_account, state, operation):
    foreign = foreign_conversation(other_account)
    leaked = observation(foreign, "foreign-op-target")
    operation.target = leaked
    operation.save(update_fields=["target"])
    result = read(full_client, conversation)
    assert result["coordination_status"] == "unavailable"
    assert result["active_operation"] is None
    assert str(leaked.pk) not in json.dumps(result)


def test_retracted_latest_target_is_hidden(full_client, conversation, state):
    ConversationMessage.objects.filter(pk=state.latest_incoming_id).update(conversation=None)
    result = read(full_client, conversation)
    assert result["coordination_status"] == "unavailable"
    assert result["work"] is None


def test_missing_active_pointer_never_reports_unknown_as_clear(full_client, conversation, state, operation):
    state.active_operation = None
    state.save(update_fields=["active_operation"])
    result = read(full_client, conversation)
    assert result["coordination_status"] == "unavailable"
    assert result["work"] is None
    operation.refresh_from_db()
    assert operation.status == "outcome_unknown"


def test_terminal_operation_in_active_pointer_is_not_claimed_valid(full_client, conversation, operation):
    operation.status = "failed"
    operation.save(update_fields=["status"])
    assert read(full_client, conversation)["coordination_status"] == "unavailable"


def test_oauth_actor_uses_same_scoped_read_contract(user, memberships, conversation, state):
    from apps.mcp.tests.test_oauth_auth import _mint_oauth_token

    user.last_workspace_id = conversation.workspace_id
    user.save(update_fields=["last_workspace_id"])
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {_mint_oauth_token(user)}")
    assert read(client, conversation)["work"]["generation"] == 3


def test_oauth_revoke_rejects_read(user, memberships, conversation):
    from oauth2_provider.models import get_access_token_model

    from apps.mcp.tests.test_oauth_auth import _mint_oauth_token

    user.last_workspace_id = conversation.workspace_id
    user.save(update_fields=["last_workspace_id"])
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {_mint_oauth_token(user)}")
    get_access_token_model().objects.filter(user=user).delete()
    status, _result = _call(client, "get_reply_coordination", {"conversation_id": str(conversation.pk)})
    assert status == 401
