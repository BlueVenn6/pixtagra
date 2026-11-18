"""Audit regressions: provider-controlled errors and redirects are untrusted."""
from types import SimpleNamespace
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
import requests

from tests.runtime.test_stage2_isolation import app_module, _bare_app


def test_worker_http_errors_do_not_echo_provider_body_or_credentials(app_module):
    app = _bare_app(app_module)
    secret = "synthetic-audit-secret-not-a-real-key"
    messages = []
    def worker():
        for code in (400, 429, 500):
            messages.append(app._categorize_http_error(
                "Moonshot Kimi", code, f"upstream echoed {secret}",
                "https://api.moonshot.cn/v1/chat/completions"))
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert len(messages) == 3
    assert all(secret not in message for message in messages)
    assert all("HTTP" in message or "endpoint" in message for message in messages)


def test_requests_cannot_forward_image_payload_on_redirect(app_module):
    app = _bare_app(app_module)
    calls = []
    session = SimpleNamespace(post=lambda *args, **kwargs: calls.append((args, kwargs)))
    app._post_if_active(session, "https://provider.example/v1", json={"image": "synthetic"})
    assert calls[0][1]["allow_redirects"] is False


def test_real_http_redirect_does_not_resend_image_body(app_module):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(307)
            self.send_header('Location', '/unapproved-target')
            self.send_header('Content-Length', '0')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app = _bare_app(app_module)
        with requests.Session() as session, pytest.raises(app_module._FatalApiError, match='redirected'):
            app._post_if_active(session, f'http://127.0.0.1:{server.server_port}/selected',
                                data=b'synthetic image content', timeout=3)
        assert received == [('/selected', b'synthetic image content')]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
