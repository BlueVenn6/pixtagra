import importlib.util
import re
from pathlib import Path



def _load_app_module():
    path = Path(__file__).resolve().parents[1] / "pixtagra.py"
    spec = importlib.util.spec_from_file_location("pixtagra_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app(module):
    app = object.__new__(module.App)
    app._config_data = {}

    class _KeyVar:
        def get(self):
            return "sk-live-secret-value"

    app.key_var = _KeyVar()

    class _ModeVar:
        def get(self):
            return "📷 General Photography"

    app.mode_var = _ModeVar()
    return app


def test_extract_json_requires_object():
    module = _load_app_module()
    assert module.extract_json('{"title": "A", "keywords": ["x"]}', require_object=True)["title"] == "A"
    assert module.extract_json('```json\n{"keywords": ["x"]}\n```', require_object=True)["keywords"] == ["x"]
    assert module.extract_json('["not", "object"]', require_object=True) is None
    assert module.extract_json('"abc"', require_object=True) is None
    assert module.extract_json("", require_object=True) is None
    assert module.extract_json("{bad", require_object=True) is None
    assert module.extract_json("plain text", require_object=True) is None


def test_extract_api_text_supports_ollama_chat_message_content():
    module = _load_app_module()
    payload = {"message": {"content": '{"title":"T","keywords":["blue square"]}'}}
    assert "blue square" in module.extract_api_text(payload, "openai_compat", "ollama")


def test_extract_api_text_supports_ollama_generate_response():
    module = _load_app_module()
    payload = {"response": '{"title":"T","keywords":["green square"]}'}
    assert "green square" in module.extract_api_text(payload, "openai_compat", "ollama")


def test_retry_status_and_retry_after_are_classified():
    module = _load_app_module()
    app = _app(module)

    class _Response:
        headers = {"Retry-After": "7"}

    assert app._is_retryable_status(429)
    assert app._is_retryable_status(500)
    assert not app._is_retryable_status(401)
    assert app._is_fatal_status(401)
    assert app._retry_after_seconds(_Response()) == 7.0


def _valid_stock_metadata():
    title = "Woman Working on Laptop at Bright Modern Home Office"
    description = (
        "A woman works on a laptop at a wooden desk in a bright modern home office. "
        "Natural window light reveals a notebook, coffee cup, and houseplant while she "
        "concentrates on remote professional tasks."
    )
    keywords = [
        "woman", "laptop", "home office", "remote work", "wooden desk", "daylight",
        "window light", "indoor", "seated", "typing", "computer", "workspace",
        "professional woman", "freelancer", "technology device", "modern interior",
        "casual clothing", "side view", "notebook", "coffee cup", "houseplant",
        "concentration", "adult", "one person", "natural lighting", "online work",
        "digital device", "table surface", "living room", "productivity", "communication",
        "domestic life", "working indoors", "screen", "chair",
    ]
    return title, description, keywords


def test_strict_metadata_quality_gate_accepts_a_complete_stock_record():
    module = _load_app_module()
    title, description, keywords = _valid_stock_metadata()
    keywords = module.rank_keywords_for_stock(keywords)

    assert module.validate_stock_metadata(title, description, keywords) == []


def test_strict_metadata_quality_gate_rejects_short_content_and_keyword_shortfall():
    module = _load_app_module()
    issues = module.validate_stock_metadata(
        "Short title",
        "One short sentence.",
        ["woman", "laptop"],
    )

    assert any(issue.startswith("Title has") for issue in issues)
    assert any(issue.startswith("Description has") for issue in issues)
    assert any("expected at least 5" in issue for issue in issues)
    assert not any("sanitization" in issue for issue in issues)  # Count alone is not a sanitizer defect.


def test_relevant_keyword_count_above_minimum_does_not_require_padding():
    module = _load_app_module()
    title, description, keywords = _valid_stock_metadata()
    hard_errors, review_warnings = module.classify_stock_metadata(
        title,
        description,
        module.rank_keywords_for_stock(keywords[:29]),
    )

    assert hard_errors == []
    assert review_warnings == []
    assert module.KEYWORD_MINIMUM_COUNT == 5
    assert module.KEYWORD_TARGET_COUNT == 35


def test_keyword_sets_below_the_recommended_target_remain_user_choice():
    module = _load_app_module()
    title, description, _keywords = _valid_stock_metadata()
    hard_errors, review_warnings = module.classify_stock_metadata(
        title,
        description,
        ["woman", "laptop"],
    )

    assert hard_errors == []
    assert any("expected at least 5" in issue for issue in review_warnings)


def test_keyword_priority_puts_subject_before_setting_and_use_case_terms():
    module = _load_app_module()
    source = ["woman", "laptop", "commercial use", "blue", "office", "copy space"]

    ranked = module.rank_keywords_for_stock(source)

    assert ranked[:2] == ["woman", "laptop"]
    assert ranked[-1] == "commercial use"


def test_commercial_background_priority_puts_surface_material_and_color_first():
    module = _load_app_module()
    source = ["product display", "blue", "marble", "copy space", "texture", "background"]

    ranked = module.rank_keywords_for_stock(source, "🎨 Commercial Background")

    assert ranked == ["blue", "marble", "copy space", "texture", "background", "product display"]
    assert ranked[-1] == "product display"


def test_metadata_quality_gate_rejects_priority_inversion():
    module = _load_app_module()
    title, description, keywords = _valid_stock_metadata()
    keywords = module.rank_keywords_for_stock(keywords)
    inverted = ["product display", *keywords]

    issues = module.validate_stock_metadata(title, description, inverted)

    assert "Keywords are not ordered by stock-search priority" in issues


def test_metadata_mode_check_rejects_mismatched_general_and_background_records():
    module = _load_app_module()

    assert module.validate_metadata_mode(
        "Woman working at home office",
        "A woman works on a laptop beside a window.",
        ["woman", "laptop", "home office"],
        "🎨 Commercial Background",
    )
    assert module.validate_metadata_mode(
        "Blue marble texture for product display background",
        "A blue marble background has a textured surface and copy space.",
        ["background", "texture", "marble", "product display"],
        "📷 General Photography",
    )
    assert module.validate_metadata_mode(
        "Blue marble texture for product display background",
        "A blue marble background has a textured surface and copy space.",
        ["background", "texture", "marble", "copy space"],
        "🎨 Commercial Background",
    ) == []


def test_compliance_warnings_cover_people_rights_editorial_and_ai_disclosure():
    module = _load_app_module()
    warnings = module.metadata_compliance_warnings(
        "Celebrity beside a visible logo and public mural",
        "An AI generated editorial illustration shows a recognizable private interior.",
        ["person", "trademark", "artwork", "news"],
        {
            "people": True,
            "visible_logo": True,
            "copyrighted_art": True,
            "private_property": True,
            "editorial_context": True,
            "ai_generated": True,
            "illustration_or_render": True,
            "mature_content": False,
        },
    )

    assert len(warnings) == 7
    assert any("model releases" in warning for warning in warnings)
    assert any("trademark" in warning for warning in warnings)
    assert any("copyright" in warning for warning in warnings)
    assert any("property release" in warning for warning in warnings)
    assert any("Editorial context" in warning for warning in warnings)
    assert any("AI-generated" in warning for warning in warnings)
    assert any("Illustration or render" in warning for warning in warnings)


def test_shutterstock_row_maps_explicit_compliance_flags_without_inventing_clearance():
    module = _load_app_module()
    row = module._build_shutterstock_csv_row(
        "editorial.jpg",
        "Public event image",
        "A public event is shown in a city street.",
        ["event", "city"],
        {
            "editorial_context": True,
            "mature_content": True,
            "illustration_or_render": True,
        },
    )

    assert row["Editorial"] == "Yes"
    assert row["Mature Content"] == "Yes"
    assert row["Illustration"] == "Yes"


def test_prompts_request_visible_evidence_compliance_flags_without_claiming_releases():
    module = _load_app_module()

    for mode in module.PROMPT_MODES:
        prompt = module.build_prompt(mode)
        assert '"compliance"' in prompt
        assert '"visible_logo"' in prompt
        assert "do not claim that a release" in prompt


def test_default_fallback_excludes_experimental_models():
    module = _load_app_module()
    app = _app(module)
    candidates = app._fallback_model_candidates("Google Gemini 2.5 Flash")
    assert "Google Gemini 2.5 Pro" not in candidates
    assert candidates == []  # No eligible automatic cloud fallback remains.
    assert "OpenAI GPT-5.5" not in candidates
    assert "Moonshot Kimi" not in candidates
    assert "Qwen 3.7 Plus" not in candidates
    assert "MiniMax M3" not in candidates
    assert "Local Ollama" not in candidates


def test_redact_secrets_masks_keys_and_query_params():
    module = _load_app_module()
    app = _app(module)
    text = "https://example.test/v1?key=sk-live-secret-value failed Authorization: Bearer sk-live-secret-value"
    redacted = app._redact_secrets(text, ["sk-live-secret-value"])
    assert "sk-live-secret-value" not in redacted
    assert "key=***" in redacted
    assert "Bearer ***" in redacted


def test_smoke_metadata_requires_json_keywords():
    module = _load_app_module()
    app = _app(module)
    ok, issue = app._smoke_metadata_ok('{"title":"T","description":"D","keywords":["blue","circle"]}')
    assert ok
    assert issue == ""
    ok, issue = app._smoke_metadata_ok("OK")
    assert not ok
    assert "valid JSON" in issue


def test_kimi_smoke_prompt_is_json_and_keyword_specific():
    module = _load_app_module()
    app = _app(module)
    prompt = app._build_smoke_prompt("Moonshot Kimi")
    assert "ONLY compact valid JSON" in prompt
    assert "keywords must be an array" in prompt
    assert "blue background" in prompt


def test_kimi_openai_compat_messages_add_strict_system_prompt():
    module = _load_app_module()
    app = _app(module)
    messages = app._openai_compat_messages("kimi", [{"type": "text", "text": "Return JSON"}])
    assert messages[0]["role"] == "system"
    assert "valid JSON only" in messages[0]["content"]
    assert messages[1]["role"] == "user"


def test_kimi_native_request_uses_json_mode_and_disables_thinking():
    module = _load_app_module()
    app = _app(module)

    class _Response:
        status_code = 200
        text = "{}"
        headers = {}

        def json(self):
            return {"choices": [{"message": {"content": '{"title":"T","description":"D","keywords":["blue","circle","test"]}'}}]}

    class _Session:
        def __init__(self):
            self.calls = []

        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append(json)
            return _Response()

    session = _Session()
    app._openai_compat_request(
        session,
        "Moonshot Kimi",
        "sk-live-secret-value",
        "Return JSON",
        "abc",
        "https://api.moonshot.cn/v1/chat/completions",
        256,
    )
    payload = session.calls[0]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_completion_tokens"] == 256
    assert "max_tokens" not in payload


def test_kimi_relay_retries_without_unsupported_json_mode():
    module = _load_app_module()
    app = _app(module)

    class _Response:
        headers = {}

        def __init__(self, status_code, text, payload=None):
            self.status_code = status_code
            self.text = text
            self._payload = payload or {}

        def json(self):
            return self._payload

    class _Session:
        def __init__(self):
            self.calls = []

        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append(json)
            if len(self.calls) == 1:
                return _Response(400, "unsupported parameter: response_format")
            return _Response(200, "{}", {"choices": [{"message": {"content": '{"title":"T","description":"D","keywords":["blue","circle","test"]}'}}]})

    session = _Session()
    _response, _payload, text, flags = app._openai_compat_request(
        session,
        "Moonshot Kimi",
        "sk-live-secret-value",
        "Return JSON",
        "abc",
        "https://relay.example.com/v1/chat/completions",
        256,
    )
    assert "response_format" in session.calls[0]
    assert "response_format" not in session.calls[1]
    assert flags["response_format_retry"]
    assert "blue" in text


def test_glm_is_not_exposed_as_a_selectable_model():
    module = _load_app_module()
    assert "Zhipu GLM-4V" not in module.API_CONFIG


def test_qwen_native_request_uses_openai_compatible_data_url_payload():
    module = _load_app_module()
    app = _app(module)

    class _Response:
        status_code = 200
        text = "{}"
        headers = {}

        def json(self):
            return {"choices": [{"message": {"content": '{"title":"T","description":"D","keywords":["blue","circle","test"]}'}}]}

    class _Session:
        def __init__(self):
            self.calls = []

        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append(json)
            return _Response()

    session = _Session()
    app._openai_compat_request(
        session,
        "Qwen 3.7 Plus",
        "qwen-key",
        "Return JSON",
        "rawbase64",
        "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
        256,
    )
    payload = session.calls[0]
    content = payload["messages"][0]["content"]
    assert payload["model"] == "qwen3.7-plus"
    assert "thinking" not in payload
    assert content[0]["image_url"]["url"] == "data:image/jpeg;base64,rawbase64"


def test_minimax_native_request_uses_openai_data_url_payload():
    module = _load_app_module()
    app = _app(module)

    class _Response:
        status_code = 200
        text = "{}"
        headers = {}

        def json(self):
            return {"choices": [{"message": {"content": '{"title":"T","description":"D","keywords":["blue","circle","test"]}'}}]}

    class _Session:
        def __init__(self):
            self.calls = []

        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append(json)
            return _Response()

    session = _Session()
    app._openai_compat_request(
        session,
        "MiniMax M3",
        "minimax-key",
        "Return JSON",
        "rawbase64",
        "https://api.minimaxi.com/v1/chat/completions",
        256,
    )
    payload = session.calls[0]
    content = payload["messages"][0]["content"]
    assert payload["model"] == "MiniMax-M3"
    assert content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_route_context_separates_direct_api_and_custom_base_url():
    module = _load_app_module()
    app = _app(module)

    direct = app._build_api_context("Google Gemini 2.5 Flash", api_key="gemini-key", base_url="")
    assert direct["route"] == "Direct API"
    assert direct["transport"] == "gemini"
    assert direct["resolved_url"].startswith("https://generativelanguage.googleapis.com/")

    relay = app._build_api_context(
        "Google Gemini 2.5 Flash",
        api_key="relay-key",
        base_url="https://relay.example.com",
    )
    assert relay["route"] == "Custom Base URL / Relay"
    assert relay["transport"] == "openai_compatible_negotiation"
    assert relay["resolved_url"] == "https://relay.example.com/v1/chat/completions"

    official_openai = app._build_api_context("OpenAI GPT-5.5", api_key="openai-key", base_url="")
    assert official_openai["route"] == "Direct API"
    assert official_openai["transport"] == "openai_responses"
    assert official_openai["resolved_url"] == "https://api.openai.com/v1/responses"


def test_connection_feedback_includes_route_transport_model_and_endpoint():
    module = _load_app_module()
    app = _app(module)
    ctx = app._build_api_context(
        "Moonshot Kimi",
        api_key="relay-key",
        base_url="https://relay.example.com/v1",
    )
    feedback = app._format_connection_feedback(
        {"ctx": ctx, "status_code": 401, "categorized_error": "auth_error"},
        "API key invalid or access denied.",
    )
    assert "API key invalid" in feedback
    assert "Route: Custom Base URL / Relay" in feedback
    assert "Transport: openai_compatible_negotiation" in feedback
    assert "Model: kimi-k2.6" in feedback
    assert "Endpoint: https://relay.example.com/v1/chat/completions" in feedback
    assert "Category: auth_error" in feedback


def test_smoke_metadata_rejects_empty_non_list_and_unusable_keywords():
    module = _load_app_module()
    app = _app(module)
    ok, issue = app._smoke_metadata_ok('{"title":"T","description":"D","keywords":[]}')
    assert not ok
    assert "fewer than 2" in issue or "not usable" in issue

    ok, issue = app._smoke_metadata_ok('{"title":"T","description":"D","keywords":"blue, circle, test"}')
    assert not ok
    assert "not a list" in issue

    ok, issue = app._smoke_metadata_ok('{"title":"T","description":"D","keywords":["测试","ai generated","creative concept"]}')
    assert not ok
    assert "not usable" in issue or "fewer than 3" in issue


def test_call_api_retries_after_retryable_429_without_real_network():
    module = _load_app_module()
    app = _app(module)
    calls = []
    sleeps = []
    original_sleep = module.time.sleep
    module.time.sleep = lambda seconds: sleeps.append(seconds)

    def fake_call_api_once(b64, model_name, api_key, base_url="", prompt=None, max_tokens=2048):
        calls.append(model_name)
        if len(calls) == 1:
            raise module._RetryableApiError("rate limited", kind="http_429", retry_after=0)
        return {
            "title": "Simple Blue Circle Test Image",
            "description": "A simple generated test image with basic shapes.",
            "keywords": ["blue", "circle", "test"],
        }

    try:
        app._call_api_once = fake_call_api_once
        result = app._call_api("abc", model_name="Google Gemini 2.5 Flash", api_key="sk-live-secret-value", base_url="")
    finally:
        module.time.sleep = original_sleep

    assert result["keywords"] == ["blue", "circle", "test"]
    assert calls == ["Google Gemini 2.5 Flash", "Google Gemini 2.5 Flash"]
    assert sleeps == [0]


def test_429_error_message_is_actionable_without_echoing_untrusted_provider_body():
    module = _load_app_module()
    app = _app(module)
    msg = app._categorize_http_error(
        "Qwen 3.7 Plus",
        429,
        '{"error":{"message":"too many requests for this new key"}}',
        "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
    )
    assert "HTTP 429 from provider" in msg
    assert "too many requests for this new key" not in msg
    assert "rate-limited" in msg
    assert "quota/billing/rate limits" in msg
    assert "quota exceeded or rate limited" not in msg


def test_smoke_error_classifies_provider_busy_body():
    module = _load_app_module()
    app = _app(module)
    category = app._classify_smoke_error(
        429,
        body='{"error":{"code":"1305","message":"该模型当前访问量过大，请您稍后再试"}}',
    )
    assert category == "provider_busy"


def test_smoke_openai_compat_retries_once_on_429_without_real_network():
    module = _load_app_module()
    app = _app(module)
    sleeps = []
    original_sleep = module.time.sleep
    module.time.sleep = lambda seconds: sleeps.append(seconds)

    class _Response:
        headers = {}

        def __init__(self, status_code, text, payload=None):
            self.status_code = status_code
            self.text = text
            self._payload = payload or {}

        def json(self):
            return self._payload

    calls = []

    class _Session:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            calls.append(json)
            if len(calls) == 1:
                return _Response(429, '{"error":{"message":"too many requests"}}')
            return _Response(200, "{}", {"choices": [{"message": {"content": '{"title":"T","description":"D","keywords":["blue","circle"]}'}}]})

    original_session = module.requests.Session
    module.requests.Session = _Session
    try:
        result = app._execute_smoke_request(
            "Qwen 3.7 Plus",
            api_key="qwen-key",
            base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        )
    finally:
        module.requests.Session = original_session
        module.time.sleep = original_sleep

    assert result["ok"]
    assert len(calls) == 2
    assert sleeps


def test_smoke_gemini_retries_timeout_without_real_network():
    module = _load_app_module()
    app = _app(module)
    sleeps = []
    original_sleep = module.time.sleep
    module.time.sleep = lambda seconds: sleeps.append(seconds)

    class _Response:
        status_code = 200
        text = "{}"
        headers = {}

        def json(self):
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"title":"T","description":"D","keywords":["blue","circle"]}'
                                }
                            ]
                        }
                    }
                ]
            }

    calls = []

    class _Session:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            calls.append((url, json, timeout))
            if len(calls) == 1:
                raise module.requests.Timeout()
            return _Response()

    original_session = module.requests.Session
    module.requests.Session = _Session
    try:
        result = app._execute_smoke_request("Google Gemini 2.5 Pro", api_key="gemini-key")
    finally:
        module.requests.Session = original_session
        module.time.sleep = original_sleep

    assert result["ok"]
    assert len(calls) == 2
    assert sleeps


def test_openai_compat_empty_first_response_tries_next_content_round():
    module = _load_app_module()
    app = _app(module)

    class _Response:
        status_code = 200
        text = "{}"
        headers = {}

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Session:
        def __init__(self):
            self.calls = []

        def post(self, url, headers=None, json=None, timeout=None, allow_redirects=True):
            assert allow_redirects is False
            self.calls.append(json)
            if len(self.calls) == 1:
                return _Response({"choices": [{"message": {"content": ""}}]})
            return _Response({"choices": [{"message": {"content": '{"title":"T","description":"D","keywords":["blue","circle","test"]}'}}]})

    session = _Session()
    response, payload, text, flags = app._openai_compat_request(
        session,
        "Moonshot Kimi",
        "sk-live-secret-value",
        "Return JSON",
        "abc",
        "https://api.moonshot.cn/v1/chat/completions",
        256,
    )
    assert response.status_code == 200
    assert len(session.calls) == 2
    assert "blue" in text
    assert flags["fallback_triggered"]


def test_visual_relevance_order_survives_generic_setting_categories():
    module = _load_app_module()
    for source in (["mountain", "forest", "lake", "clouds", "sky"],
                   ["forest", "path", "trees", "green", "outdoors"],
                   ["ceramic bowl", "soil", "seedlings", "garden", "wood"]):
        assert module.rank_keywords_for_stock(source) == source


def test_prompts_separate_generation_target_from_acceptance_minimum():
    module = _load_app_module()
    for mode in module.PROMPT_MODES:
        for language, label in (("en", "English"), ("zh", "Simplified Chinese")):
            prompt = module.build_prompt(mode, language)
            assert f"Draft 25 to 35 {label} keywords" in prompt
            assert "Final acceptance is 20 to 35" in prompt
            assert "Target 25 to 30 retained" in prompt
            assert "tag35" not in prompt
            assert re.search(r"(?<!\d)5 to 35", prompt) is None
            assert "first 10" in prompt
            assert "Never pad" in prompt
            assert "image-specific relevance" in prompt
