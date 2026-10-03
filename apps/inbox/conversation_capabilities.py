"""Explicit DM capabilities, independent from display-platform names.

This is a local implementation contract, not a statement about future API
availability or this account's granted scopes. A future Threads DM adapter must
be explicitly implemented and verified before changing the unsupported gate.
"""

_DM_HISTORY_ADAPTERS = frozenset({"facebook", "instagram_login"})


def conversation_capabilities(platform: str) -> dict:
    implemented = platform in _DM_HISTORY_ADAPTERS
    return {
        "platform": platform,
        "dm_history_adapter": "implemented_observation_only" if implemented else "not_implemented",
        "dm_history_complete": False,
        "native_outgoing_observation": implemented,
        "v2_send_preconditions": False,
        "account_permissions_verified": False,
        "unsupported_reason": (
            ""
            if implemented
            else "No verified DM adapter in this version; no fallback to another platform or UI scraping."
        ),
    }
