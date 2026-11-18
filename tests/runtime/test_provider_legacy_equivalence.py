"""Differential request tests against the frozen pre-extraction operation.

Both paths use real routing/negotiation helpers and the same fake HTTP server
responses. No external requests, GUI, user configuration or credentials.
"""
import copy
import json
from pathlib import Path
import threading
from types import MethodType

import pytest
import requests

from tests.runtime.test_stage2_isolation import app_module, _bare_app


METADATA = {
    "title": "Blue circle in a simple geometric design",
    "description": "Synthetic description with punctuation: café, blue.",
    "keywords": ["blue circle", "geometric design", "background"],
    "compliance": {"visible_logo": False},
    "keyword_review": {"status": "pending"},
    "extension_field": {"preserved": True},
}
OLD_OPERATION = Path(__file__).parents[1] / "fixtures/provider_legacy/call_api_once_30a744f.py.txt"


class Response:
    def __init__(self, status=200, text=None, payload=None):
        self.status_code = status
        self.headers = {"Retry-After": "7"}
        self.text = text if text is not None else json.dumps(payload)
        self.payload = payload

    def json(self):
        if self.payload is None:
            raise ValueError("Synthetic invalid JSON")
        return copy.deepcopy(self.payload)


@pytest.mark.parametrize("model,base_url", [
    ("OpenAI GPT-5.5", ""),
    ("OpenAI GPT-5.5", "https://relay.example/v1"),
    ("Moonshot Kimi", ""),
    ("Qwen 3.7 Plus", ""),
    ("MiniMax M3", ""),
    ("Google Gemini 2.5 Flash", ""),
])
@pytest.mark.parametrize("case", [
    "success", "review", "401", "404", "429", "503", "non_json",
    "bad_schema", "bad_json", "timeout", "network", "cancelled", "redirect",
])
def test_cloud_operation_matches_frozen_legacy(app_module, monkeypatch, model, base_url, case):
    namespace = dict(vars(app_module))
    exec(compile(OLD_OPERATION.read_text(encoding="utf-8"), str(OLD_OPERATION), "exec"), namespace)
    original = namespace["_call_api_once"]

    def run(operation):
        app = _bare_app(app_module)
        app.stop_flag = case == "cancelled"
        app.is_processing = True
        app.error_lock = threading.Lock()
        app.api_errors = []
        # Exercise a custom ID without changing the model catalog.
        app._config_data = {"model_overrides": {model: "synthetic-vision-id"}}
        transcript, logs = [], []
        app._log_api_event = lambda *args, **kwargs: logs.append(copy.deepcopy((args, kwargs)))

        class Session:
            def __init__(self):
                self.headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, **kwargs):
                transcript.append(copy.deepcopy((url, kwargs)))
                if case == "timeout":
                    raise requests.Timeout("synthetic")
                if case == "network":
                    raise requests.ConnectionError("synthetic-key must be redacted")
                if case.isdigit():
                    return Response(int(case), text='{"error":"synthetic service failure"}')
                if case == "redirect":
                    return Response(307, text="redirect")
                if case == "non_json":
                    return Response(text="not JSON")
                result = {"checks": [{"id": 0, "verdict": "supported"}]} if case == "review" else METADATA
                if case == "bad_schema":
                    result = {"title": "Title", "keywords": [5]}
                reply = "{invalid JSON" if case == "bad_json" else json.dumps(result)
                # Same decoded content in every supported legacy response envelope.
                return Response(payload={
                    "output_text": reply,
                    "choices": [{"message": {"content": reply}}],
                    "candidates": [{"content": {"parts": [{"text": reply}]}}],
                    "usage": {"input_tokens": 23, "output_tokens": 9, "total_tokens": 32},
                })

        monkeypatch.setattr(app_module.requests, "Session", Session)
        try:
            output = MethodType(operation, app)(
                "synthetic-base64", model, "synthetic-key", base_url=base_url,
                prompt="EXACT LEGACY PROMPT", max_tokens=345,
                response_kind="keyword_review" if case == "review" else "metadata",
            )
            outcome = ("result", output)
        except (app_module._FatalApiError, app_module._RetryableApiError) as exc:
            outcome = (type(exc).__name__, str(exc), exc.code,
                       getattr(exc, "kind", None), getattr(exc, "retry_after", None))
        return outcome, transcript, logs, app.api_errors

    before = run(original)
    after = run(app_module.App._call_api_once)
    assert after == before
    if case in {"success", "review"}:
        assert after[0][0] == "result"
        assert len(after[1]) == 1
    if case == "cancelled":
        assert not after[1]
    if case == "network":
        assert "synthetic-key" not in after[0][1]
