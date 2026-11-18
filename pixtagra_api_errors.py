"""Stable, redacted API failure codes shared by transports and reports."""

from __future__ import annotations

import re

ERROR_CODES = frozenset({
    "auth_error", "permission_denied", "rate_limited", "model_unavailable", "endpoint_error",
    "vision_unsupported", "timeout", "network_error", "non_json",
    "invalid_schema", "image_not_accepted", "cancelled", "unknown",
    "server_error", "provider_busy", "missing_api_key", "empty_response",
})


def classify_api_error(*, kind="", status_code=None, body=""):
    text = re.sub(r"\s+", " ", str(body or "")).lower()
    if status_code is None and kind.startswith("http_"):
        try:
            status_code = int(kind[5:])
        except ValueError:
            pass
    if kind in {"cancelled", "cancel"}:
        return "cancelled"
    if status_code == 403 or kind == "permission_denied" or "permission denied" in text:
        return "permission_denied"
    if status_code == 401 or kind in {"auth", "auth_error"}:
        return "auth_error"
    if status_code == 429 or kind in {"rate_limited", "http_429"}:
        return "rate_limited"
    if any(x in text for x in ("model not found", "model_not_found", "no available channel", "not found the model", "unavailable for this provider account", "not available on this relay")):
        return "model_unavailable"
    if kind in {"vision_unsupported", "image_not_accepted"} or any(x in text for x in ("does not support vision", "unsupported image", "image_url")):
        return "vision_unsupported" if kind != "image_not_accepted" else "image_not_accepted"
    if kind in {"timeout", "timed_out"} or "timed out" in text:
        return "timeout"
    if kind in {"network_error", "no_response"} or "network error" in text:
        return "network_error"
    if kind in {"non_json"}:
        return "non_json"
    if kind in {"schema_mismatch", "invalid_json", "invalid_schema"}:
        return "invalid_schema"
    if "non-json" in text:
        return "non_json"
    if "api key required" in text:
        return "missing_api_key"
    if any(x in text for x in ("endpoint assembly", "resolved url is invalid", "workspace compatible endpoint")):
        return "endpoint_error"
    if status_code in (400, 404, 405, 406, 410, 415) or kind.startswith("http_4"):
        return "endpoint_error"
    if status_code is not None and status_code >= 500:
        return "server_error"
    if kind in ERROR_CODES:
        return kind
    return "unknown"


class FatalApiError(Exception):
    """Permanent failure: retries cannot fix the same request or credential."""
    def __init__(self, message, code=None, status_code=None):
        super().__init__(message)
        self.code = code or classify_api_error(status_code=status_code, body=message)


class RetryableApiError(Exception):
    """Transient failure; legacy kind is retained for diagnostic compatibility."""
    def __init__(self, message, kind="retryable", retry_after=None, code=None):
        super().__init__(message)
        self.kind = kind
        self.code = code or classify_api_error(kind=kind, body=message)
        self.retry_after = retry_after
