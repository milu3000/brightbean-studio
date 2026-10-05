from unittest.mock import MagicMock

import pytest

from providers.exceptions import APIError
from providers.meta_insights import fetch_insights_safe, parse_insights_response


def _resp(data):
    return MagicMock(json=MagicMock(return_value=data))


def test_fetch_insights_safe_skips_invalid_metric_errors():
    request = MagicMock(
        side_effect=[
            APIError(
                "Meta API error 400: invalid metric",
                platform="Facebook",
                status_code=400,
                raw_response={"error": {"code": 100, "message": "The value must be a valid insights metric"}},
            ),
            _resp({"data": [{"name": "page_media_view", "values": [{"value": 10}]}]}),
        ]
    )

    values, errors = fetch_insights_safe(
        request,
        platform="Facebook",
        endpoint="https://graph.facebook.com/v25.0/page-1/insights",
        access_token="page-token",
        metrics=["bad_metric", "page_media_view"],
    )

    assert values == {"page_media_view": 10}
    assert set(errors) == {"bad_metric"}
    assert errors["bad_metric"] == "unsupported_metric"


def test_fetch_insights_safe_reraises_permission_errors():
    permission_error = APIError(
        "Meta API error 400: missing permission",
        platform="Facebook",
        status_code=400,
        raw_response={
            "error": {
                "code": 200,
                "type": "OAuthException",
                "message": "Permissions error: read_insights is required.",
            }
        },
    )
    request = MagicMock(side_effect=permission_error)

    with pytest.raises(APIError) as excinfo:
        fetch_insights_safe(
            request,
            platform="Facebook",
            endpoint="https://graph.facebook.com/v25.0/page-1/insights",
            access_token="page-token",
            metrics=["page_media_view"],
        )

    assert excinfo.value is permission_error


def test_parse_insights_response_prefers_lifetime_when_metric_name_repeats():
    values = parse_insights_response(
        {
            "data": [
                {
                    "name": "post_media_view",
                    "period": "lifetime",
                    "values": [{"value": 32741}],
                },
                {
                    "name": "post_total_media_view_unique",
                    "period": "lifetime",
                    "values": [{"value": 21105}],
                },
                {
                    "name": "post_total_media_view_unique",
                    "period": "day",
                    "values": [
                        {"value": 0, "end_time": "2026-06-21T07:00:00+0000"},
                        {"value": 0, "end_time": "2026-06-22T07:00:00+0000"},
                    ],
                },
            ]
        }
    )

    assert values == {
        "post_media_view": 32741,
        "post_total_media_view_unique": 21105,
    }


def _fetch(request, platform="Instagram (Direct)", endpoint_type="media"):
    return fetch_insights_safe(
        request,
        platform=platform,
        endpoint="https://graph.example.test/object/insights",
        access_token="fake-secret-token",
        metrics=["views", "reach"],
        endpoint_type=endpoint_type,
    )


@pytest.mark.parametrize(
    "error",
    [
        {"code": 190},
        {"code": 10},
        {"code": 200},
        {"code": 100, "error_subcode": 33, "message": "Unsupported get request due to missing permissions"},
        {"code": 100, "message": "Tried accessing nonexisting field insights"},
        {"code": 100, "message": "Unknown parameter"},
        {"code": 100, "message": "The value must be a valid insights metric", "is_transient": True},
    ],
)
@pytest.mark.parametrize("platform", ["Facebook", "Instagram", "Instagram (Direct)", "Threads"])
def test_fetch_does_not_swallow_non_metric_failures(error, platform):
    exc = APIError("secret-bearing error", status_code=400, raw_response={"error": error})
    request = MagicMock(side_effect=exc)
    with pytest.raises(APIError) as raised:
        _fetch(request, platform)
    assert raised.value is exc
    assert request.call_count == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_fetch_propagates_http_auth_unknown_and_transient_failures(status):
    exc = APIError("failure", status_code=status)
    with pytest.raises(APIError) as raised:
        _fetch(MagicMock(side_effect=exc))
    assert raised.value is exc


def test_fetch_raises_instead_of_success_when_every_metric_is_unsupported():
    exc = APIError(
        "private response",
        status_code=400,
        raw_response={"error": {"code": 100, "message": "The value must be a valid insights metric"}},
    )
    request = MagicMock(side_effect=exc)
    with pytest.raises(APIError, match="No supported analytics metrics returned") as raised:
        _fetch(request)
    assert request.call_count == 2
    assert raised.value.raw_response == {}
    assert "private" not in str(raised.value)


def test_partial_metric_values_do_not_hide_later_auth_failure():
    exc = APIError("expired", raw_response={"error": {"code": 190}})
    request = MagicMock(side_effect=[_resp({"data": [{"name": "views", "values": [{"value": 10}]}]}), exc])
    with pytest.raises(APIError) as raised:
        _fetch(request)
    assert raised.value is exc


@pytest.mark.parametrize(
    "message",
    [
        "The value must be a valid insights metric",
        "(#100) metric must be one of the following values: views,reach",
        "The following metrics (impressions) are not supported for this media type",
    ],
)
def test_unsupported_metric_diagnostic_is_safe_in_result_and_logs(message, caplog):
    secret = "private-token-should-not-leak"
    exc = APIError(secret, status_code=400, raw_response={"error": {"code": 100, "message": message, "token": secret}})
    request = MagicMock(side_effect=[exc, _resp({"data": [{"name": "reach", "values": [{"value": 7}]}]})])
    values, errors = _fetch(request)
    assert values == {"reach": 7}
    assert errors == {"views": "unsupported_metric"}
    assert secret not in repr((values, errors))
    assert secret not in caplog.text


def test_unknown_empty_metrics_are_not_invented_from_invalid_endpoint():
    exc = APIError("nonexisting field insights", raw_response={"error": {"code": 100}})
    with pytest.raises(APIError) as raised:
        _fetch(MagicMock(side_effect=exc))
    assert raised.value is exc
