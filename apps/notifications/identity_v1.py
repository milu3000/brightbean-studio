"""Stable notification identity contract, also used by its additive migration."""

import hashlib
import json


def opaque_id(value):
    if not isinstance(value, str) or not value or len(value) > 255:
        return ""
    return "" if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) else value


def digest(parts):
    return hashlib.sha256(json.dumps([str(item) for item in parts], ensure_ascii=False).encode()).hexdigest()


def canonical_identity(conversation):
    thread = opaque_id(conversation.platform_conversation_id)
    if not thread and conversation.identity_kind == "verified_peer" and not conversation.peer_ambiguous:
        peer = opaque_id(conversation.peer_id)
        thread = "peer:" + peer if peer else ""
    return digest(
        [
            conversation.workspace_id,
            conversation.social_account_id,
            conversation.platform,
            "dm",
            thread or "conversation:" + str(conversation.pk),
        ]
    )


def message_identity(message, conversation=None):
    if conversation is not None:
        return canonical_identity(conversation)
    extra = message.extra if isinstance(message.extra, dict) else {}
    domain = message.message_type
    thread = opaque_id(extra.get("conversation_id")) if domain == "dm" else ""
    if domain in {"comment", "mention"}:
        thread = opaque_id(extra.get("root_comment_id")) or opaque_id(extra.get("thread_id"))
        current, seen = message, set()
        while not thread and current.pk not in seen and len(seen) < 50:
            seen.add(current.pk)
            metadata = current.extra if isinstance(current.extra, dict) else {}
            parent_id = opaque_id(metadata.get("parent_id"))
            parents = type(message).objects.filter(
                workspace_id=message.workspace_id, social_account_id=message.social_account_id, message_type=domain
            )
            parent = (
                parents.filter(pk=current.parent_message_id).first()
                if current.parent_message_id
                else parents.filter(platform_message_id=parent_id).first()
                if parent_id
                else None
            )
            if parent is None:
                thread = opaque_id(current.platform_message_id) or str(current.pk)
                break
            current = parent
        post = opaque_id(extra.get("post_id")) or str(message.related_post_id or "")
        thread = post + ":" + (thread or "message:" + str(message.pk))
    return digest(
        [
            message.workspace_id,
            message.social_account_id,
            message.social_account.platform,
            domain,
            thread or "message:" + str(message.pk),
        ]
    )


def event_identity(account_id, platform, message_id, domain="dm"):
    return digest([account_id, platform, domain, message_id])
