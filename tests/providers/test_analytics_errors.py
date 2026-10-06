"""Post unavailability is not evidence that an entire OAuth grant is broken."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict

import httpx
import pytest

from providers.analytics_errors import classify_analytics_error
from providers.exceptions import APIError, OAuthError, QuotaExceededError, RateLimitError, TokenExpiredError

META_PLATFORMS = ["threads", "instagram", "instagram_login", "facebook"]
ALL_PLATFORMS = META_PLATFORMS + ["youtube", "tiktok"]


def _meta_error(code=100, subcode=33, message=None, **extra):
    error = {
        "code": code,
        "message": message
        or (
            "Unsupported get request. Object with ID '123' does not exist, "
            "cannot be loaded due to missing permissions, or does not support this operation."
        ),
        **extra,
    }
    if subcode is not None:
        error["error_subcode"] = subcode
    return APIError(
        "The API response may contain a token; never keep this message", status_code=400, raw_response={"error": error}
    )


def _google_error(reason, status=403):
    return APIError(
        "Forbidden",
        status_code=status,
        raw_response={"error": {"code": status, "message": "Forbidden", "errors": [{"reason": reason}]}},
    )


@pytest.mark.parametrize("platform", META_PLATFORMS)
def test_unsupported_object_with_missing_permissions_is_only_post_inaccessible(platform):
    result = classify_analytics_error(_meta_error(), platform)
    assert result.category == "post_inaccessible"
    assert result.code == 100
    assert result.subcode == 33
    assert not result.is_account_error


@pytest.mark.parametrize("platform", META_PLATFORMS)
def test_unsupported_object_does_not_prove_account_scope_even_in_account_context(platform):
    assert classify_analytics_error(_meta_error(), platform, context="account").category == "unknown"


def test_unsupported_object_without_subcode_uses_exact_provider_message_shape():
    assert classify_analytics_error(_meta_error(subcode=None), "threads").category == "post_inaccessible"
    assert (
        classify_analytics_error(_meta_error(subcode=None, message="Invalid metric parameter"), "threads").category
        == "unknown"
    )


@pytest.mark.parametrize("code", [10, 200, "10", "200"])
@pytest.mark.parametrize("platform", META_PLATFORMS)
def test_meta_generic_permission_code_depends_on_call_context(code, platform):
    exc = _meta_error(code=code, subcode=None, message="Application does not have permission for this action")
    assert classify_analytics_error(exc, platform, context="post").category == "post_inaccessible"
    assert classify_analytics_error(exc, platform, context="account").category == "account_scope"


@pytest.mark.parametrize(
    ("platform", "scope"),
    [
        ("threads", "threads_manage_insights"),
        ("instagram", "instagram_manage_insights"),
        ("instagram_login", "instagram_business_manage_insights"),
        ("facebook", "read_insights"),
    ],
)
@pytest.mark.parametrize("prefix", ["Requires ", "(#200) Requires the ", "Missing ", "Missing permission "])
def test_meta_explicit_required_analytics_scope_is_account_scope_from_post_call(platform, scope, prefix):
    exc = _meta_error(code=200, subcode=None, message=f"{prefix}{scope} permission to access insights")
    result = classify_analytics_error(exc, platform)
    assert result.category == "account_scope"
    assert result.signal == "required_analytics_scope"


@pytest.mark.parametrize(
    "scope", ["threads_manage_replies", "instagram_manage_insights", "threads_manage_insights_extra"]
)
def test_wrong_or_partial_scope_name_does_not_escalate(scope):
    exc = _meta_error(code=200, subcode=None, message=f"Requires {scope} permission")
    assert classify_analytics_error(exc, "threads").category == "post_inaccessible"


@pytest.mark.parametrize(
    "message",
    [
        "This operation does not require threads_manage_insights",
        "The token already has threads_manage_insights permission",
        "Unsupported get request due to missing permissions. See threads_manage_insights",
        "Permission denied: example requires threads_manage_insights",
    ],
)
def test_scope_name_mentioned_without_affirmative_requirement_is_not_missing_scope(message):
    exc = _meta_error(code=200, subcode=None, message=message)
    assert classify_analytics_error(exc, "threads").category == "post_inaccessible"


def test_unsupported_object_prose_cannot_smuggle_in_scope_classification():
    exc = _meta_error(message="Unsupported get request. Missing threads_manage_insights permissions.")
    assert classify_analytics_error(exc, "threads").category == "post_inaccessible"


@pytest.mark.parametrize("field", ["missing_scopes", "missing_permissions"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_structured_missing_required_scope_survives_post_context(field, wrapped):
    body = {field: ["threads_manage_insights"]}
    if wrapped:
        body = {"error": body}
    exc = APIError("No message needed", raw_response=body)
    assert classify_analytics_error(exc, "threads").category == "account_scope"


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
@pytest.mark.parametrize("context", ["post", "account"])
def test_typed_token_failure_always_stays_account_wide(platform, context):
    result = classify_analytics_error(TokenExpiredError("expired"), platform, context=context)
    assert result.category == "account_auth"
    assert result.is_account_error


@pytest.mark.parametrize("platform", META_PLATFORMS)
@pytest.mark.parametrize("code", [190, "190", 102])
def test_meta_global_token_codes_stay_account_auth(platform, code):
    exc = _meta_error(code=code, subcode=463, message="Expired token")
    assert classify_analytics_error(exc, platform).category == "account_auth"


@pytest.mark.parametrize(
    "body", [{"error": "invalid_grant"}, {"error": {"code": "invalid_grant"}}, {"code": "invalid_token"}]
)
def test_structured_oauth_failures_are_account_auth(body):
    assert (
        classify_analytics_error(OAuthError("refresh failed", raw_response=body), "youtube").category == "account_auth"
    )


def test_typed_oauth_exact_invalid_grant_fallback_is_preserved_without_message_evidence():
    result = classify_analytics_error(OAuthError("Token refresh failed: invalid_grant"), "youtube")
    assert result.category == "account_auth"
    assert result.safe_evidence == {"signal": "oauth_invalid_grant"}
    assert classify_analytics_error(APIError("Token refresh failed: invalid_grant"), "youtube").category == "unknown"
    assert classify_analytics_error(OAuthError("not_invalid_grant_code"), "youtube").category == "unknown"


@pytest.mark.parametrize("reason", ["authError", "authenticationFailure", "UNAUTHENTICATED"])
def test_google_auth_reasons_are_account_wide(reason):
    assert classify_analytics_error(_google_error(reason), "youtube").category == "account_auth"


def test_google_status_without_legacy_reason_is_recognized():
    exc = APIError("denied", raw_response={"error": {"status": "UNAUTHENTICATED"}})
    assert classify_analytics_error(exc, "youtube").category == "account_auth"


def test_google_insufficient_permissions_is_not_silently_hidden():
    result = classify_analytics_error(_google_error("insufficientPermissions"), "youtube")
    assert result.category == "account_scope"
    assert result.reason == "insufficientpermissions"


def test_google_error_info_definitive_scope_reason_is_recognized():
    exc = APIError(
        "Request had insufficient authentication scopes.",
        status_code=403,
        raw_response={
            "error": {
                "status": "PERMISSION_DENIED",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}
                ],
            }
        },
    )
    assert classify_analytics_error(exc, "youtube").category == "account_scope"


@pytest.mark.parametrize("reason", ["forbidden", "videoNotFound", "notFound"])
def test_google_post_access_errors_do_not_prove_scope_or_deletion(reason):
    assert classify_analytics_error(_google_error(reason), "youtube").category == "post_inaccessible"
    assert classify_analytics_error(_google_error(reason), "youtube", context="account").category == "unknown"


@pytest.mark.parametrize("code", ["scope_not_authorized", "scope_permission_missed"])
@pytest.mark.parametrize("wrapped", [True, False])
def test_tiktok_definitive_scope_codes_take_precedence_over_http_401(code, wrapped):
    body = {"code": code}
    exc = APIError("denied", status_code=401, raw_response={"error": body} if wrapped else body)
    assert classify_analytics_error(exc, "tiktok").category == "account_scope"


def test_tiktok_invalid_token_is_account_auth():
    exc = APIError("denied", raw_response={"error": {"code": "access_token_invalid"}})
    assert classify_analytics_error(exc, "tiktok").category == "account_auth"


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
def test_plain_http_401_is_account_auth(platform):
    assert classify_analytics_error(APIError("denied", status_code=401), platform).category == "account_auth"


@pytest.mark.parametrize("status", [404, 410])
@pytest.mark.parametrize("platform", ALL_PLATFORMS)
def test_missing_or_gone_post_never_implies_archived_or_deleted(platform, status):
    exc = APIError("This post may be archived or deleted", status_code=status)
    assert classify_analytics_error(exc, platform).category == "post_inaccessible"
    assert classify_analytics_error(exc, platform, context="account").category == "unknown"


@pytest.mark.parametrize("state", ["archived", "deleted"])
def test_precise_post_state_requires_explicit_provider_normalization(state):
    exc = APIError("No raw prose is trusted", status_code=404)
    exc.analytics_post_state = state
    assert classify_analytics_error(exc, "threads").category == f"post_{state}"
    assert classify_analytics_error(exc, "threads", context="account").category == "unknown"


def test_authoritative_auth_failure_is_not_hidden_by_normalized_post_state():
    exc = _meta_error(code=190)
    exc.analytics_post_state = "archived"
    assert classify_analytics_error(exc, "threads").category == "account_auth"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.parametrize("platform", ALL_PLATFORMS)
def test_rate_and_server_errors_are_transient(platform, status):
    assert classify_analytics_error(APIError("permission error", status_code=status), platform).category == "transient"


@pytest.mark.parametrize("error_type", [RateLimitError, QuotaExceededError])
def test_typed_quota_errors_do_not_escalate_from_forbidden_prose(error_type):
    assert (
        classify_analytics_error(error_type("Forbidden due to missing quota permission"), "youtube").category
        == "transient"
    )


@pytest.mark.parametrize(
    "reason", ["quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded", "backendError"]
)
def test_google_403_quota_and_backend_reasons_remain_transient(reason):
    assert classify_analytics_error(_google_error(reason), "youtube").category == "transient"


@pytest.mark.parametrize("code", ["rate_limit_exceeded", "internal_error"])
def test_tiktok_transient_codes(code):
    exc = APIError("failed", raw_response={"error": {"code": code}})
    assert classify_analytics_error(exc, "tiktok").category == "transient"


@pytest.mark.parametrize("code", [1, 2, 4, 17, 32, 341, 613])
def test_meta_transient_codes(code):
    assert classify_analytics_error(_meta_error(code=code, subcode=None), "threads").category == "transient"


def test_meta_explicit_transient_boolean_is_not_inferred_from_string():
    assert classify_analytics_error(_meta_error(is_transient=True), "threads").category == "transient"
    assert classify_analytics_error(_meta_error(is_transient="true"), "threads").category == "post_inaccessible"


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        ConnectionError(),
        httpx.ReadTimeout("private URL"),
        httpx.ConnectError("private URL"),
        httpx.RemoteProtocolError("bad response"),
    ],
)
def test_network_errors_are_transient(exc):
    assert classify_analytics_error(exc, "threads").category == "transient"


@pytest.mark.parametrize(
    "message", ["permission", "forbidden", "insufficient scope", "(#10)", "(#200)", "Archived", "deleted"]
)
@pytest.mark.parametrize("context", ["post", "account"])
def test_bare_message_markers_are_not_authoritative(message, context):
    assert classify_analytics_error(APIError(message), "threads", context=context).category == "unknown"


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
def test_generic_403_remains_unknown(platform):
    assert classify_analytics_error(APIError("forbidden", status_code=403), platform).category == "unknown"


@pytest.mark.parametrize(
    "body",
    [None, [], "error", {"error": []}, {"error": None}, {"error": {"code": []}}, {"error": {"errors": "malformed"}}],
)
def test_malformed_payloads_are_unknown_without_crashing(body):
    exc = APIError("permission")
    exc.raw_response = body
    assert classify_analytics_error(exc, "youtube").category == "unknown"


def test_codes_are_platform_specific():
    assert classify_analytics_error(_meta_error(code=190), "youtube").category == "unknown"
    assert classify_analytics_error(_google_error("insufficientPermissions"), "threads").category == "unknown"
    exc = APIError("denied", raw_response={"error": {"code": "scope_not_authorized"}})
    assert classify_analytics_error(exc, "facebook").category == "unknown"


def test_evidence_is_scalar_allowlisted_and_result_is_immutable():
    secret = "fake-secret-do-not-retain"
    exc = _meta_error(message=f"Unsupported get request. {secret}", access_token=secret, fbtrace_id=secret)
    exc.raw_response["private_user_data"] = secret
    before = deepcopy(exc.raw_response)
    result = classify_analytics_error(exc, "threads")
    assert result.safe_evidence == {"http_status": 400, "code": 100, "subcode": 33, "signal": "meta_object_unavailable"}
    assert secret not in repr(asdict(result))
    assert exc.raw_response == before
    with pytest.raises(FrozenInstanceError):
        result.category = "account_scope"
    result.safe_evidence["code"] = "mutated copy"
    assert result.code == 100


@pytest.mark.parametrize(
    "value", [True, 190.0, "190secret", "1234567890123456789", "https://example.test/?access_token=secret", [], {}]
)
def test_unsafe_codes_are_not_retained_or_coerced(value):
    exc = APIError("do not copy", raw_response={"error": {"code": value, "error_subcode": value, "reason": value}})
    result = classify_analytics_error(exc, "threads")
    assert result.category == "unknown"
    assert result.code is result.subcode is result.reason is None


def test_unknown_string_reason_and_status_do_not_leak():
    exc = APIError("secret", raw_response={"error": {"errors": [{"reason": "secret"}], "status": "secret"}})
    assert classify_analytics_error(exc, "youtube").safe_evidence == {"signal": "unclassified"}


def test_generic_exception_message_is_never_read():
    class UnprintableError(Exception):
        def __str__(self):
            raise AssertionError("Classifier must not copy a raw error")

    assert classify_analytics_error(UnprintableError(), "threads").category == "unknown"


def test_provider_display_name_preserves_direct_instagram_scope_classification():
    exc = _meta_error(code=200, subcode=None, message="Requires instagram_business_manage_insights permission")
    assert classify_analytics_error(exc, "Instagram (Direct)").category == "account_scope"


def test_meta_named_scope_is_required_suffix_is_explicit_scope_evidence():
    exc = _meta_error(code=200, subcode=None, message="Permissions error: read_insights is required.")
    assert classify_analytics_error(exc, "Facebook").category == "account_scope"


def test_normalized_authoritative_post_state_can_refine_ambiguous_unavailability():
    exc = _meta_error()
    exc.analytics_post_state = "archived"
    assert classify_analytics_error(exc, "threads").category == "post_archived"


def test_invalid_context_is_rejected():
    with pytest.raises(ValueError, match="context"):
        classify_analytics_error(_meta_error(code=200), "threads", context="posts")
