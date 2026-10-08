"""Read-only proof of the newest timeline snapshot returned to this actor."""

from django.core import signing

from .canonical_access import digest, narrow_scope

SALT = "brightbean.inbox.composer-observation.v1"


def _owner_proof(conversation):
    """Snapshot existing dispatch fences without acquiring a grant or a lock."""
    from apps.social_accounts.models import SocialAccount

    from .models import DMConversationOwnership, DMSendControl

    owner = (
        DMConversationOwnership.objects.filter(conversation_id=conversation.pk)
        .values(
            "id",
            "control_id",
            "workspace_id",
            "social_account_id",
            "platform",
            "account_platform_id",
            "platform_conversation_id",
            "peer_id",
            "identity_kind",
            "owner_scope",
            "epoch",
            "paused",
            "resume_cutoff",
        )
        .first()
    )
    control = (
        DMSendControl.objects.filter(social_account_id=conversation.social_account_id)
        .values(
            "id",
            "workspace_id",
            "social_account_id",
            "platform",
            "account_platform_id",
            "epoch",
            "paused",
            "resume_cutoff",
            "coverage_from",
            "coverage_version",
        )
        .first()
    )
    if owner is None and control is None:
        return None
    account = (
        SocialAccount.objects.filter(
            pk=conversation.social_account_id, workspace_id=conversation.workspace_id, platform=conversation.platform
        )
        .values_list("account_platform_id", flat=True)
        .first()
    )
    expected = (conversation.workspace_id, conversation.social_account_id, conversation.platform, account)
    invalid = account is None or (
        control is not None
        and tuple(control[key] for key in ("workspace_id", "social_account_id", "platform", "account_platform_id"))
        != expected
    )
    if owner is not None:
        invalid = (
            invalid
            or control is None
            or tuple(owner[key] for key in ("workspace_id", "social_account_id", "platform", "account_platform_id"))
            != expected
            or (owner["control_id"], owner["platform_conversation_id"], owner["peer_id"], owner["identity_kind"])
            != (
                control["id"] if control else None,
                conversation.platform_conversation_id,
                conversation.peer_id,
                conversation.identity_kind,
            )
        )
    if invalid:
        # Do not expose another scope's identifiers even for corrupt links.
        return {"invalid": True, "state": digest([owner, control])}
    return {
        "owner_id": str(owner["id"]) if owner else None,
        "owner_epoch": owner["epoch"] if owner else None,
        "control_id": str(control["id"]) if control else None,
        "control_epoch": control["epoch"] if control else None,
        "state": digest([owner, control]),
    }


def issue_observation(scope_stamp, conversation, messages):
    from .canonical_reads import _identity

    return signing.dumps(
        {
            "scope": scope_stamp,
            "conversation": str(conversation.pk),
            "identity": _identity(conversation),
            "revision": conversation.revision,
            "incoming_generation": conversation.incoming_generation,
            "page_kind": "newest",
            "rendered_rows": digest([item["id"] for item in messages]),
            "owner_proof": _owner_proof(conversation),
        },
        salt=SALT,
        compress=True,
    )


def verify_composer_observation(scope, conversation_id, token):
    from .canonical_reads import CanonicalReadError, _denied, _identity, _recheck, _scope_filter, _snapshot, _uuid
    from .models import InboxConversation

    scope = narrow_scope(scope, target=(InboxConversation, conversation_id))
    accounts, stamp = _snapshot(scope)
    current = InboxConversation.objects.filter(_scope_filter(scope, accounts), pk=_uuid(conversation_id)).first()
    if current is None:
        raise _denied()
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ValueError
        value = signing.loads(token, salt=SALT, max_age=3600)
        revision, generation = value["revision"], value["incoming_generation"]
        owner_proof = _owner_proof(current)
        if (
            value["scope"] != stamp
            or value["conversation"] != str(current.pk)
            or value["identity"] != _identity(current)
            or value["page_kind"] != "newest"
            or not isinstance(value["rendered_rows"], str)
            or len(value["rendered_rows"]) != 64
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or revision != current.revision
            or generation != current.incoming_generation
            or value["owner_proof"] != owner_proof
            or (owner_proof is not None and owner_proof.get("invalid"))
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, signing.BadSignature) as exc:
        raise CanonicalReadError(
            "stale_observation", "The displayed conversation changed; reopen its newest page."
        ) from exc
    _recheck(scope, stamp)
    if not InboxConversation.objects.filter(
        _scope_filter(scope, accounts),
        pk=current.pk,
        revision=revision,
        incoming_generation=generation,
        social_account_id=current.social_account_id,
        platform=current.platform,
        platform_conversation_id=current.platform_conversation_id,
        peer_id=current.peer_id,
        peer_ambiguous=current.peer_ambiguous,
        conversation_type=current.conversation_type,
    ).exists():
        raise CanonicalReadError("stale_observation", "The displayed conversation changed while checking.")
    if _owner_proof(current) != owner_proof:
        raise CanonicalReadError("stale_observation", "The inbox dispatch fence changed while checking; reload.")
    return {
        "conversation_id": str(current.pk),
        "revision": revision,
        "incoming_generation": generation,
        "owner_proof": owner_proof,
    }
