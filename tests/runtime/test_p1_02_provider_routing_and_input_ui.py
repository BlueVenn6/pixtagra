"""Contracts for the selectable cloud models and editable PixTagra controls.

These tests exercise only local configuration and fake HTTP sessions.  They do
not read a user key, call a provider, create a Tk window, or access a real
credential store.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from tests.runtime.test_stage2_isolation import _Var, _bare_app, app_module


def test_visible_model_catalog_matches_the_validated_provider_kit(app_module):
    expected_visible = {
        "OpenAI GPT-5.6 Sol",
        "OpenAI GPT-5.6 Terra",
        "OpenAI GPT-5.6 Luna",
        "Moonshot Kimi",
        "Qwen 3.7 Plus",
        "MiniMax M3",
        "Local Ollama",
    }
    visible = set(app_module.SELECTABLE_MODELS)

    assert "Google Gemini 2.5 Flash" not in visible
    assert "Google Gemini 2.5 Pro" not in visible
    assert "Zhipu GLM-4V" not in visible
    assert "OpenAI GPT-5" not in visible
    assert "OpenAI GPT-5 mini" not in visible
    assert "OpenAI GPT-5.5 mini" not in visible
    assert visible == expected_visible
    assert app_module.API_CONFIG["OpenAI GPT-5.5"]["model"] == "gpt-5.5"
    assert app_module.API_CONFIG["OpenAI GPT-5.6 Sol"]["model"] == "gpt-5.6-sol"
    assert app_module.API_CONFIG["OpenAI GPT-5.6 Terra"]["model"] == "gpt-5.6-terra"
    assert app_module.API_CONFIG["OpenAI GPT-5.6 Luna"]["model"] == "gpt-5.6-luna"
    assert app_module.API_CONFIG["Qwen 3.7 Plus"]["provider"] == "qwen"
    assert app_module.API_CONFIG["MiniMax M3"]["provider"] == "minimax"


def test_native_and_relay_routes_are_assembled_per_provider_contract(app_module):
    app = _bare_app(app_module)

    qwen_native = app._build_api_context("Qwen 3.7 Plus", api_key="synthetic-key")
    assert qwen_native["route"] == "Direct API"
    assert qwen_native["transport"] == "openai_compat"
    assert qwen_native["resolved_url"] == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )

    minimax_native = app._build_api_context("MiniMax M3", api_key="synthetic-key")
    assert minimax_native["route"] == "Direct API"
    assert minimax_native["resolved_url"] == "https://api.minimaxi.com/v1/chat/completions"

    openai_native = app._build_api_context("OpenAI GPT-5.5", api_key="synthetic-key")
    assert openai_native["route"] == "Direct API"
    assert openai_native["transport"] == "openai_responses"
    assert openai_native["resolved_url"] == "https://api.openai.com/v1/responses"

    relay = app._build_api_context(
        "OpenAI GPT-5.5",
        api_key="synthetic-key",
        base_url="https://relay.example.test/v1",
    )
    assert relay["route"] == "Custom Base URL / Relay"
    assert relay["transport"] == "openai_compatible_negotiation"
    assert relay["resolved_url"] == "https://relay.example.test/v1/responses"


def test_qwen_uses_the_provider_kit_dashscope_default_without_a_workspace_url(app_module):
    app = _bare_app(app_module)
    ctx = app._build_api_context("Qwen 3.7 Plus", api_key="synthetic-key")

    assert ctx["endpoint_issue"] == ""
    assert ctx["resolved_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


def test_relay_model_unavailable_error_distinguishes_a_relay_from_a_provider_failure(app_module):
    app = _bare_app(app_module)
    message = app._categorize_http_error(
        "OpenAI GPT-5.5",
        503,
        '{"error":{"code":"model_not_found","message":"No available channel for model gpt-5.5"}}',
        "https://relay.example.test/v1/responses",
    )

    assert "not available on this relay route" in message
    assert "clear Base URL" in message


def test_openai_responses_payload_matches_the_provider_kit(app_module):
    app = _bare_app(app_module)

    class Response:
        status_code = 200
        text = "{}"
        headers = {}

        @staticmethod
        def json():
            return {"output_text": '{"title":"T","description":"D","keywords":["blue","circle","test"]}'}

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append((url, json))
            return Response()

    session = Session()
    response, payload, text, flags = app._openai_responses_request(
        session,
        "OpenAI GPT-5.5",
        "synthetic-key",
        "Return JSON.",
        "synthetic-base64",
        "https://api.openai.com/v1/responses",
        128,
    )

    request_url, request_payload = session.calls[0]
    assert response.status_code == 200
    assert payload == response.json()
    assert "blue" in text
    assert flags["protocol"] == "responses"
    assert request_url == "https://api.openai.com/v1/responses"
    assert request_payload["model"] == "gpt-5.5"
    assert request_payload["max_output_tokens"] == 128
    assert "messages" not in request_payload
    content = request_payload["input"][0]["content"]
    assert content[0] == {
        "type": "input_image",
        "image_url": "data:image/jpeg;base64,synthetic-base64",
        "detail": "high",
    }
    assert content[1] == {"type": "input_text", "text": "Return JSON."}


def test_chat_compatible_payloads_use_provider_kit_direct_endpoints(app_module):
    app = _bare_app(app_module)

    class Response:
        status_code = 200
        text = "{}"
        headers = {}

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": '{"title":"T","description":"D","keywords":["blue","circle","test"]}'}}]}

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append((url, json))
            return Response()

    expected = {
        "Qwen 3.7 Plus": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "MiniMax M3": "https://api.minimaxi.com/v1/chat/completions",
    }
    for model_name, url in expected.items():
        session = Session()
        app._openai_compat_request(
            session, model_name, "synthetic-key", "Return JSON.", "synthetic-base64", url, 128
        )
        request_url, payload = session.calls[0]
        assert request_url == url
        assert payload["model"] == app_module.API_CONFIG[model_name]["model"]
        assert payload["max_tokens"] == 128
        assert payload["messages"][-1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_proxy_protocol_negotiation_follows_provider_kit_retry_boundary(app_module):
    app = _bare_app(app_module)

    class Response:
        headers = {}

        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = "{}"

        def json(self):
            return self._payload

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append((url, json))
            if url.endswith("/responses"):
                return Response(404, {"error": {"message": "unsupported endpoint"}})
            return Response(200, {"choices": [{"message": {"content": "fallback ok"}}]})

    session = Session()
    response, _payload, text, flags = app._openai_compatible_negotiated_request(
        session,
        "OpenAI GPT-5.5",
        "synthetic-key",
        "Return JSON.",
        "synthetic-base64",
        "https://relay.example.test/v1",
        128,
    )

    assert response.status_code == 200
    assert text == "fallback ok"
    assert flags["protocol_fallback"]
    assert [url for url, _payload in session.calls] == [
        "https://relay.example.test/v1/responses",
        "https://relay.example.test/v1/chat/completions",
    ]

    class AuthSession(Session):
        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append((url, json))
            return Response(401, {"error": {"message": "invalid key"}})

    auth_session = AuthSession()
    response, _payload, _text, flags = app._openai_compatible_negotiated_request(
        auth_session,
        "OpenAI GPT-5.5",
        "synthetic-key",
        "Return JSON.",
        "synthetic-base64",
        "https://relay.example.test/v1",
        128,
    )
    assert response.status_code == 401
    assert not flags["protocol_fallback"]
    assert len(auth_session.calls) == 1


def test_smoke_analysis_and_repair_use_the_same_responses_transport(app_module):
    app = _bare_app(app_module)
    calls = []

    class Response:
        status_code = 200
        text = "{}"
        headers = {}

        @staticmethod
        def json():
            return {
                "output_text": (
                    '{"title":"Simple geometric image","description":"A blue and orange geometric test image.",'
                    '"keywords":["blue background","orange circle","green rectangle"]}'
                )
            }

    class Session:
        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            calls.append((url, json))
            return Response()

    original_session = app_module.requests.Session
    app_module.requests.Session = Session
    try:
        smoke = app._execute_smoke_request("OpenAI GPT-5.5", api_key="synthetic-key")
        analysis = app._call_api_once(
            "synthetic-base64",
            "OpenAI GPT-5.5",
            "synthetic-key",
            prompt="Return JSON.",
        )
        repaired = app._repair_api_call(
            "synthetic-base64",
            ["blue background"],
            [],
            2,
            app_module.PROVIDERS["OpenAI GPT-5.5"],
            app_module.API_CONFIG["OpenAI GPT-5.5"],
            "synthetic-key",
            model_name="OpenAI GPT-5.5",
        )
    finally:
        app_module.requests.Session = original_session

    assert smoke["ok"]
    assert analysis["title"] == "Simple geometric image"
    assert repaired == ["blue background", "orange circle", "green rectangle"]
    assert len(calls) == 3
    assert {url for url, _payload in calls} == {"https://api.openai.com/v1/responses"}
    assert all("input" in payload and "messages" not in payload for _url, payload in calls)


def test_calendar_popup_geometry_is_anchored_and_clamped_to_the_screen(app_module):
    geometry = app_module._date_picker_geometry

    # A normal desktop position opens immediately beneath the date button.
    assert geometry(1200, 100, 40, 40, 356, 348, 0, 0, 1920, 1080) == (884, 146)

    # Near the bottom edge it opens above the same control, without leaving the screen.
    x, y = geometry(1200, 900, 40, 40, 356, 348, 0, 0, 1366, 1000)
    assert 8 <= x <= 1002
    assert y == 546

    # A monitor to the left of the primary display retains valid negative coordinates.
    x, y = geometry(-1700, 100, 40, 40, 356, 348, -1920, 0, 1920, 1080)
    assert -1912 <= x <= -364
    assert y == 146


def test_main_window_geometry_is_centered_and_clamped(app_module):
    geometry = app_module._centered_window_geometry

    assert geometry(1400, 900, 0, 0, 1920, 1080) == (1400, 900, 260, 90)

    width, height, x, y = geometry(1400, 900, 0, 0, 1024, 768)
    assert (width, height) == (992, 736)
    assert (x, y) == (16, 16)

    width, height, x, y = geometry(1400, 900, -1920, 0, 1920, 1080)
    assert (width, height, x, y) == (1400, 900, -1660, 90)


def test_ctk_window_geometry_does_not_apply_high_dpi_scaling_twice(app_module):
    class ScaledWindow:
        @staticmethod
        def _get_window_scaling():
            return 2.25

    geometry = app_module._centered_ctk_window_geometry

    assert geometry(ScaledWindow(), 1400, 900, (0, 0, 2880, 1812)) == (
        1248, 773, 36, 36
    )
    assert geometry(ScaledWindow(), 1400, 900, (-2880, 0, 2880, 1812)) == (
        1248, 773, -2844, 36
    )


def test_workspace_uses_readable_editor_fonts_and_non_clipping_action_layout(app_module):
    workspace_source = inspect.getsource(app_module.App._setup_workspace)
    card_source = inspect.getsource(app_module.ImageCard)

    assert 'font=("Segoe UI", 13)' in card_source
    assert 'font=("Segoe UI", 12)' in card_source
    # Equal-width columns squeezed long action labels at high DPI. The
    # replacement keeps each label whole and wraps at the available width;
    # the real GUI probe additionally asserts rendered text is not clipped.
    assert 'toolbar = ActionRow(' in workspace_source
    assert 'outputbar = ActionRow(' in workspace_source
    assert 'batchbar = ActionRow(' in workspace_source
    assert 'cfgbar.add(cfgc)' in workspace_source
    assert 'bc.add(self.btns[f"filter_{key}"])' in workspace_source


def test_edit_controls_bind_a_native_right_click_context_menu(app_module):
    app_source = inspect.getsource(app_module.App)
    card_source = inspect.getsource(app_module.ImageCard)

    assert "def _bind_edit_context_menu" in app_source
    assert "<Button-3>" in app_source
    assert "<<Paste>>" in app_source
    assert 'action == "select_all"' in app_source
    assert "self.shoot_date_picker_btn" in app_source
    assert "picker.withdraw()" in app_source
    assert "_widget_monitor_work_area(anchor)" in app_source
    assert "picker.winfo_reqwidth()" in app_source
    assert app_source.index("render_days()") < app_source.index("picker.deiconify()")
    assert "self.app._bind_edit_context_menu(widget)" in card_source
    # The main window must be visible during normal startup. A previous
    # withdraw/deiconify startup sequence left packaged windows hidden.
    assert "self.withdraw()" not in app_source
    assert "self._center_main_window()" in app_source
    assert "self.deiconify()" not in app_source
    assert "self.minsize(min(1200, width), min(700, height))" in app_source
    assert "api_actions = ctk.CTkFrame" in app_source


def test_touch_input_controls_offer_windows_keyboard_fallback(app_module):
    app_source = inspect.getsource(app_module.App)
    candidates = app_module._touch_keyboard_candidates()

    assert any(path.name == "TabTip.exe" for path in candidates)
    assert any(path.name == "osk.exe" for path in candidates)
    assert "GetSystemMetrics(95)" in inspect.getsource(app_module._touch_keyboard_supported)
    assert "<FocusIn>" in app_source
    assert "<Button-1>" in app_source
    assert "creationflags=getattr(subprocess, \"CREATE_NO_WINDOW\", 0)" in app_source


def test_saved_key_load_control_uses_verified_reveal_and_does_not_persist_plaintext(app_module):
    app_source = inspect.getsource(app_module.App)
    assert 'text="Load saved key"' in app_source
    assert 'command=self._load_saved_key' in app_source
    assert 'self._start_credential_operation("reveal"' in app_source
    assert "secret_for_authorized_use" in app_source
    assert "Saved key loaded for session" in app_source
    assert "secret_supplier=lambda: self.key_var.get()" in app_source


def test_saved_key_load_populates_only_the_masked_session_input(app_module, monkeypatch):
    app = _bare_app(app_module, key="")
    app.model_var = _Var("Qwen 3.7 Plus")
    app.__dict__["credential_status_lbl"] = None

    class Lease:
        def __init__(self):
            self.closed = False

        def secret_for_authorized_use(self):
            return "saved-key-from-windows"

        def close(self):
            self.closed = True

    lease = Lease()
    warnings = []
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda *args: warnings.append(args))

    app._on_saved_key_load_result(SimpleNamespace(status="authorized", lease=lease))

    assert app.key_var.get() == "saved-key-from-windows"
    assert app._credential_status_text == "Saved key loaded for session"
    assert lease.closed
    assert warnings == []


@pytest.mark.parametrize(
    ("model_name", "provider"),
    (("Qwen 3.7 Plus", "qwen"), ("MiniMax M3", "minimax")),
)
def test_new_cloud_models_use_the_same_session_credential_flow(app_module, model_name, provider):
    app = _bare_app(app_module, key="synthetic-key")
    calls = []

    class Coordinator:
        def set_session_key(self, provider_id, _secret):
            calls.append(provider_id)
            return SimpleNamespace(status="session_key_set")

    app.model_var = _Var(model_name)
    app._credential_coordinator = Coordinator()
    # This bare App deliberately bypasses CTk.__init__; avoid Tk's missing
    # widget proxy while exercising only the provider-to-session handoff.
    app.__dict__["credential_status_lbl"] = None
    app._on_key_input_changed()
    app._sync_input_to_session_key(provider)

    assert calls == [provider, provider]
