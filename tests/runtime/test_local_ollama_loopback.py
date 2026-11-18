"""Actual loopback HTTP through production connection and image-generation paths.

The server speaks Ollama's chat protocol; it does not run a real language model.
All images/metadata are synthetic. No API keys or cloud requests are used.
"""
import base64
import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PIL import Image

from tests.runtime.test_stage2_isolation import app_module, _bare_app
from tests.runtime.test_export_delivery import metadata as english_metadata
from tests.test_language_support import chinese_metadata


@pytest.fixture
def ollama_server():
    state = SimpleNamespace(status=200, body=None, requests=[])
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append((self.path, payload, dict(self.headers)))
            body = state.body
            if body is None:
                prompt = payload["messages"][0]["content"]
                data = chinese_metadata() if "Simplified Chinese" in prompt else english_metadata()
                body = {"model": payload["model"], "message": {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)}, "done": True}
            raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if state.status == 302:
                self.send_header("Location", "/should-not-follow")
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("suffix", ["", "/", "/api", "/api/chat", "/api/generate", "/proxy/api/chat"])
def test_custom_port_connection_uses_ollama_chat_and_real_image(app_module, ollama_server, suffix):
    app = _bare_app(app_module, key="")
    result = app._execute_smoke_request("Local Ollama", local_url=ollama_server.url + suffix)
    assert result["ok"] and result["smoke_request_sent"]
    path, payload, headers = ollama_server.requests[0]
    assert path == ("/proxy/api/chat" if suffix.startswith("/proxy") else "/api/chat")
    assert payload["model"] == "llava" and payload["stream"] is False
    image = Image.open(io.BytesIO(base64.b64decode(payload["messages"][0]["images"][0])))
    image.verify()
    assert "Authorization" not in headers and len(ollama_server.requests) == 1


@pytest.mark.parametrize("language", ["en", "zh"])
def test_local_generation_uses_selected_language_without_cloud_or_key(app_module, tmp_path, ollama_server, monkeypatch, language):
    app = _bare_app(app_module, key="")
    app.error_lock, app.api_errors = threading.Lock(), []
    app._batch_language, app._batch_visual_review_enabled = language, False
    app._checkpoint_result = lambda *args: None
    app._call_api = lambda *args, **kwargs: pytest.fail("Local generation must not call a cloud provider")
    monkeypatch.setattr(app_module, "RETRY_DELAYS", [0])
    source = tmp_path / "local.jpg"
    Image.new("RGB", (96, 64), "white").save(source)
    result = app._process(SimpleNamespace(path=str(source)), "Local Ollama", app_module.PROMPT_MODES[0], "", ollama_server.url)
    assert result and result["metadata_language"] == language
    expected = chinese_metadata() if language == "zh" else english_metadata()
    assert result["keywords"] == expected["keywords"]
    assert result["title"] == expected["title"]
    assert not app.api_errors and len(ollama_server.requests) == 1
    assert result["keyword_review"]["status"] == "model_unqualified"


@pytest.mark.parametrize("status,body,category", [
    (404, {"error": "model 'llava' not found, try pulling it first"}, "model_unavailable"),
    (400, {"error": "model does not support images"}, "vision_unsupported"),
    (200, "not-json", "non_json"),
    (200, {"message": {"content": ""}}, None),
])
def test_local_connection_reports_real_http_and_model_errors(app_module, ollama_server, status, body, category):
    app = _bare_app(app_module, key="")
    ollama_server.status, ollama_server.body = status, body
    result = app._execute_smoke_request("Local Ollama", local_url=ollama_server.url)
    assert not result["ok"] and result["message"]
    assert result["status_code"] == status
    if category:
        assert result["categorized_error"] == category


def test_unstarted_service_is_not_reported_as_connected(app_module):
    app = _bare_app(app_module, key="")
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    result = app._execute_smoke_request("Local Ollama", local_url=f"http://127.0.0.1:{port}")
    assert not result["ok"] and result["categorized_error"] == "network_error"


def test_preflight_allows_configured_local_model_loading_time(app_module, ollama_server):
    app = _bare_app(app_module, key="")
    timeouts = []
    original = app._post_if_active
    def request(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return original(*args, **kwargs)
    app._post_if_active = request
    assert app._execute_smoke_request("Local Ollama", local_url=ollama_server.url)["ok"]
    assert timeouts == [(app_module.HTTP_CONNECT_TIMEOUT, app_module.OLLAMA_READ_TIMEOUT)]


def test_local_redirect_does_not_resend_image(app_module, ollama_server):
    ollama_server.status, ollama_server.body = 302, {"error": "redirect"}
    with pytest.raises(RuntimeError, match="redirected"):
        app_module.call_local_model("synthetic", "prompt", "llava", ollama_server.url + "/api/chat")
    assert len(ollama_server.requests) == 1


def test_default_url_matches_ollama_port_and_missing_scheme_is_reported(app_module):
    app = _bare_app(app_module, key="")
    assert app._resolve_local_api_url("") == "http://localhost:11434/api/chat"
    result = app._execute_smoke_request("Local Ollama", local_url="localhost:11434")
    assert not result["ok"] and result["categorized_error"] == "endpoint_error"


@pytest.mark.parametrize("address", ["http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:abc", "ftp://localhost:11434", "http://localhost:11434?x=1", "http://localhost:11434/#fragment"])
def test_invalid_local_address_is_blocked_before_request(app_module, address):
    app = _bare_app(app_module, key="")
    result = app._execute_smoke_request("Local Ollama", local_url=address)
    assert not result["ok"] and not result["smoke_request_sent"]
    assert result["categorized_error"] == "endpoint_error"


@pytest.mark.parametrize("address", ["http://192.168.1.20:11434", "http://[::1]:11434/api/chat", "https://ollama.example/proxy/api/chat"])
def test_lan_ipv6_and_proxy_syntax_is_retained_without_contacting_them(app_module, address):
    app = _bare_app(app_module, key="")
    assert not app._local_endpoint_issue(address)
    assert app._resolve_local_api_url(address).endswith("/api/chat")
