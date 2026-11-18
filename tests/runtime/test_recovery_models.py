"""Model identity survives real config I/O; all transport responses are synthetic."""
import json
import threading

import pytest
import requests

from pixtagra_models import OPENAI_GPT_56_CANDIDATES, LEGACY_GPT_56_SELECTIONS
from tests.runtime.test_stage2_isolation import app_module, _legacy_config_app, _Var
from tests.runtime.test_provider_legacy_equivalence import Response, METADATA


def config_app(module):
    app = _legacy_config_app(module)
    app.visual_review_var = _Var(True)
    app._legacy_config_key_present = False
    return app


@pytest.mark.parametrize("name,model_id", (*OPENAI_GPT_56_CANDIDATES, *LEGACY_GPT_56_SELECTIONS))
@pytest.mark.parametrize("custom", [None, "my-provider/custom-vision-model"])
def test_saved_model_survives_save_and_fresh_app_load(app_module, name, model_id, custom):
    config = {"model": name, "provider_base_urls": {"openai": "https://relay.example/v1"}}
    if custom:
        config["model_overrides"] = {name: custom}
    app_module.CONFIG_FILE.write_text(json.dumps(config), encoding="utf-8")
    app = config_app(app_module)
    app._load_config()
    assert app.__dict__.get("_config_load_error") is None
    assert app.model_var.get() == name
    assert app._resolve_request_model(name) == (custom or model_id)
    assert app._save_config()
    restarted = config_app(app_module)
    restarted._load_config()
    assert restarted.model_var.get() == name
    assert restarted._resolve_request_model(name) == (custom or model_id)
    assert restarted.base_url_var.get() == "https://relay.example/v1"
    if (name, model_id) in LEGACY_GPT_56_SELECTIONS:
        assert name not in app_module.SELECTABLE_MODELS


@pytest.mark.parametrize("name,model_id", OPENAI_GPT_56_CANDIDATES)
@pytest.mark.parametrize("base_url", ["", "https://relay.example/v1"])
def test_new_models_use_exact_id_and_image_in_generation_and_connection_test(app_module, monkeypatch, name, model_id, base_url):
    app = config_app(app_module)
    app.stop_flag = False
    app.error_lock = threading.Lock()
    app.api_errors = []
    calls = []

    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            assert kwargs["allow_redirects"] is False
            return Response(payload={"output_text": json.dumps(METADATA)})

    monkeypatch.setattr(app_module.requests, "Session", Session)
    result = app._call_api_once("synthetic-image", name, "synthetic-key", base_url=base_url)
    assert result == METADATA  # Preserve extension fields and downstream dict shape.
    smoke = app._execute_smoke_request(name, api_key="synthetic-key", base_url=base_url)
    assert smoke["ok"]
    assert len(calls) == 2
    for url, kwargs in calls:
        assert url == (base_url or "https://api.openai.com/v1") + "/responses"
        assert kwargs["json"]["model"] == model_id
        assert any(p["type"] == "input_image" for item in kwargs["json"]["input"] for p in item["content"])


@pytest.mark.parametrize("status,body,expected", [
    (401, "invalid key", "auth_error"),
    (403, "access denied", "permission_denied"),
    (404, "model_not_found", "model_unavailable"),
])
def test_connection_errors_distinguish_credentials_permissions_and_models(app_module, monkeypatch, status, body, expected):
    app = config_app(app_module)
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, *args, **kwargs): return Response(status, text=body)
    monkeypatch.setattr(app_module.requests, "Session", Session)
    result = app._execute_smoke_request(OPENAI_GPT_56_CANDIDATES[0][0], api_key="synthetic-key")
    assert not result["ok"]
    assert result["categorized_error"] == expected


def test_network_connection_failure_is_not_key_failure(app_module, monkeypatch):
    app = config_app(app_module)
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, *args, **kwargs): raise requests.ConnectionError("offline")
    monkeypatch.setattr(app_module.requests, "Session", Session)
    result = app._execute_smoke_request(OPENAI_GPT_56_CANDIDATES[0][0], api_key="synthetic-key")
    assert not result["ok"]
    assert result["categorized_error"] == "network_error"
