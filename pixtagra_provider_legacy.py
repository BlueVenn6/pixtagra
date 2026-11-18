"""First migration boundary: execute one existing cloud operation without Tk.

Transport negotiation is injected, not reimplemented. The caller still owns
retry, credential authorization, image preparation, prompts and result merging.
"""
from dataclasses import dataclass, field
from typing import Callable

import requests

from pixtagra_api_errors import FatalApiError, RetryableApiError
from pixtagra_models import extract_api_text, extract_json
from pixtagra_provider import ModelDiscovery, ProviderRequest, ProviderResult, TokenUsage


@dataclass(frozen=True, repr=False)
class LegacyOperations:
    """Explicit transitional dependencies, never an App reference or secret."""
    session_factory: Callable
    family_request: Callable
    post_if_active: Callable
    log_event: Callable
    record_error: Callable
    categorize_http_error: Callable
    is_retryable_status: Callable
    is_fatal_status: Callable
    retry_after_seconds: Callable
    looks_like_missing_image_reply: Callable
    log_debug_preview: Callable
    metadata_schema_issue: Callable
    redact_secrets: Callable


@dataclass(frozen=True, repr=False)
class LegacyProvider:
    operations: LegacyOperations = field(repr=False)

    def list_models(self):
        # Discovery is optional; manual configuration remains available.
        return ModelDiscovery()

    def generate(self, request: ProviderRequest, *, api_key: str) -> ProviderResult:
        op = self.operations
        model_name = request.model_name
        op.log_event("api_start")
        try:
            with op.session_factory() as session:
                session.headers.update({"Connection": "close"})
                if request.transport in (
                    "openai_compat", "openai_responses", "openai_compatible_negotiation"
                ):
                    r, j, txt, flags = op.family_request(session, request, api_key)
                    op.log_event("api_done", **flags)
                    if r is None:
                        raise RetryableApiError(f"{model_name} failed before receiving an HTTP response.", kind="no_response")
                elif request.transport == "gemini":
                    r = op.post_if_active(session,
                        f"{request.endpoint}?key={api_key}",
                        json={"contents": [{"parts": [{"text": request.prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": request.image_base64}}]}]},
                        timeout=request.timeout
                    )
                    try:
                        j = r.json() if r.status_code == 200 else None
                    except ValueError:
                        j = None
                    txt = extract_api_text(j, request.transport, request.provider) if j else ""
                else:
                    op.record_error(f"Unsupported provider: {request.provider}")
                    return ProviderResult(None, request.provider, request.model_id)

                if r.status_code != 200:
                    msg = op.categorize_http_error(model_name, r.status_code, (r.text or "")[:300], request.endpoint)
                    print(msg)
                    if op.is_retryable_status(r.status_code):
                        raise RetryableApiError(msg, kind=f"http_{r.status_code}", retry_after=op.retry_after_seconds(r))
                    if op.is_fatal_status(r.status_code):
                        raise FatalApiError(msg, status_code=r.status_code)
                    raise RetryableApiError(msg, kind=f"http_{r.status_code}")

                if j is None:
                    msg = f"{model_name} returned a non-JSON HTTP response."
                    print(msg)
                    raise RetryableApiError(msg, kind="non_json")

            print(f"[api] response_chars={len(txt or '')}")
            if not txt:
                keys = list(j.keys()) if isinstance(j, dict) else []
                print(f"[api] empty text from {request.transport} keys={keys}")
                raise RetryableApiError(f"{model_name} returned an empty response body.", kind="empty_response")
            elif request.transport in (
                "openai_compat", "openai_responses", "openai_compatible_negotiation"
            ) and op.looks_like_missing_image_reply(txt):
                raise RetryableApiError(f"{model_name} relay did not accept the attached image after fallback retries.", kind="image_not_accepted")
            result = extract_json(txt, require_object=True)
            if txt and result is None:
                msg = f"{model_name} returned text, but JSON parsing failed."
                op.log_debug_preview("extract_json_failed", txt, api_key=api_key, limit=200)
                raise RetryableApiError(msg, kind="invalid_json")
            issue = op.metadata_schema_issue(result) if request.response_kind == "metadata" else None
            if issue:
                raise RetryableApiError(f"{model_name} {issue}.", kind="schema_mismatch")
            return ProviderResult(result, request.provider, request.model_id, TokenUsage.from_response(j))
        except requests.Timeout:
            raise RetryableApiError(
                f"{model_name} timed out ({request.timeout[0]}s connect, {request.timeout[1]}s read).",
                kind="timeout",
            )
        except requests.RequestException as e:
            raise RetryableApiError(f"{model_name} network error: {op.redact_secrets(e, [api_key])}", kind="network_error")
