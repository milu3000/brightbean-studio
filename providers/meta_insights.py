"""Helpers shared by Meta-backed providers."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from .analytics_errors import AnalyticsErrorContext, classify_analytics_error
from .exceptions import APIError

logger = logging.getLogger(__name__)


def parse_insights_response(data: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    periods: dict[str, str] = {}
    for entry in data.get("data", []):
        name = entry.get("name", "")
        if not name:
            continue
        period = entry.get("period", "")
        if periods.get(name) == "lifetime" and period != "lifetime":
            continue
        if "total_value" in entry:
            value = entry.get("total_value", {}).get("value", 0)
        else:
            value = entry.get("values", [{}])[0].get("value", 0)
        if name not in values or period == "lifetime":
            values[name] = value
            periods[name] = period
            continue
    return values


def _is_unsupported_metric_error(exc: APIError) -> bool:
    """Only a code-100 metric validation refusal is safe to skip individually.

    A missing insights edge is an endpoint failure, not an unsupported metric.
    Likewise, code 100/33's permission wording says nothing about metrics.
    """
    error = exc.raw_response.get("error") if isinstance(exc.raw_response, dict) else None
    if not isinstance(error, dict) or error.get("code") not in (100, "100"):
        return False
    if error.get("error_subcode") in (33, "33"):
        return False
    message = error.get("message")
    if not isinstance(message, str):
        return False
    message = message.lower()
    return bool(
        "must be a valid insights metric" in message
        or re.search(r"(?:^|\(#[0-9]+\)\s*)metric must be one of (?:the following|these) values\b", message)
        or re.search(r"\b(?:metric|metrics)\b[^.!?]{0,160}\b(?:is|are) not supported\b", message)
    )


def is_missing_meta_field(exc: APIError, field: str) -> bool:
    """Recognize a specific unavailable Graph field, never an absent object."""
    error = exc.raw_response.get("error") if isinstance(exc.raw_response, dict) else None
    if not isinstance(error, dict) or error.get("code") not in (100, "100"):
        return False
    if error.get("error_subcode") in (33, "33"):
        return False
    message = error.get("message")
    if not isinstance(message, str):
        return False
    return bool(re.search(rf"\bnonexisting field\s+\(?{re.escape(field)}\)?(?![a-z0-9_])", message.lower()))


def fetch_insights_safe(
    request: Callable[..., Any],
    *,
    platform: str,
    endpoint: str,
    access_token: str,
    metrics: list[str],
    base_params: dict[str, Any] | None = None,
    metric_params: dict[str, dict[str, Any]] | None = None,
    endpoint_type: str = "insights",
) -> tuple[dict[str, Any], dict[str, str]]:
    """Allow partial supported metrics without hiding object or account errors.

    Failed metrics retain a stable code only. Raw API messages can contain
    access tokens and must never become snapshot data or log text here.
    """
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for metric in metrics:
        params = {**(base_params or {}), **(metric_params or {}).get(metric, {}), "metric": metric}
        try:
            resp = request("GET", endpoint, access_token=access_token, params=params)
        except APIError as exc:
            context: AnalyticsErrorContext = "account" if endpoint_type in {"account", "page"} else "post"
            classification = classify_analytics_error(exc, platform, context=context)
            if classification.category != "unknown" or not _is_unsupported_metric_error(exc):
                raise
            errors[metric] = "unsupported_metric"
            logger.warning(
                "Skipping unsupported %s %s metric %s: category=%s evidence=%s",
                platform,
                endpoint_type,
                metric,
                classification.category,
                classification.safe_evidence,
            )
            continue
        values.update(parse_insights_response(resp.json()))
    if errors and not values:
        # Returning an empty mapping here would make providers manufacture
        # successful zero-valued analytics from a completely failed fetch.
        raise APIError("No supported analytics metrics returned", platform=platform)
    return values, errors
