"""Behavioral regressions from the large-batch and quality audit."""
import io
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from tests.runtime.test_stage2_isolation import app_module, _bare_app


def test_visual_review_uses_same_provider_image_and_one_request(app_module):
    app = _bare_app(app_module)
    calls = []
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"checks": [
            {"id": 0, "verdict": "supported", "reason": "A mask is visible"},
            {"id": 1, "verdict": "unsupported", "reason": "No snow is visible"},
        ]}
    app._call_api_once = request
    data = {"title": "Existing title", "keywords": ["mask", "snow"]}
    result = app._review_generated_keywords(data, "image", "Qwen 3.7 Plus", "synthetic", "https://relay.test/v1", {"type": "cloud"})
    assert len(calls) == 1
    assert calls[0][0] == ("image", "Qwen 3.7 Plus", "synthetic", "https://relay.test/v1")
    assert calls[0][1]["response_kind"] == "keyword_review"
    assert result["keywords"] == ["mask"] and result["title"] == "Existing title"


@pytest.mark.parametrize("mode", ["invalid", "network", "cancelled", "local"])
@pytest.mark.parametrize("policy_version", [None, 2])
def test_visual_review_failure_preserves_keywords_and_reports_unverified(app_module, mode, policy_version):
    app = _bare_app(app_module)
    app.error_lock = threading.Lock()
    app.api_errors = []
    calls = []
    def request(*args, **kwargs):
        calls.append(True)
        if mode == "network":
            raise RuntimeError("Synthetic timeout")
        return {"checks": []}
    app._call_api_once = request
    app.stop_flag = mode == "cancelled"
    data = {"keywords": ["mask"]}
    if policy_version is not None:
        data["keyword_policy_version"] = policy_version
    result = app._review_generated_keywords(data, "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "local" if mode == "local" else "cloud"})
    assert result["keywords"] == ["mask"]
    assert app_module.keyword_review_warnings(result)
    assert len(calls) == (0 if mode in {"cancelled", "local"} else 1)
    assert bool(app.api_errors) == (mode in {"invalid", "network"})


def test_keyword_repair_rechecks_near_duplicates_across_original_and_new(app_module):
    app = _bare_app(app_module)
    app._repair_api_call = lambda *args: ["mountains", "lake", "river", "forest"]
    result = app._apply_sanitizer_and_repair(
        {"title": "Mountain and Lake Landscape beneath Clouds in Evening Light",
         "description": "Mountains rise above a lake with trees along the water and clouds overhead. Evening light falls across the landscape, showing the rocky slopes and the still water in a wide view.",
         "keywords": ["mountain"]},
        "image", {"type": "cloud"}, "synthetic", "", "", "OpenAI GPT-5.5",
    )
    assert "mountain" in result["keywords"]
    assert "mountains" not in result["keywords"]
    assert "lake" in result["keywords"]
    _, rejected, _ = app_module.sanitize_keywords_for_stock(result["keywords"])
    assert rejected == []


@pytest.mark.parametrize("keyword", [123, {"subject": "mountain"}, None, True])
def test_model_schema_rejects_non_text_keywords(app_module, keyword):
    app = _bare_app(app_module)
    assert app._metadata_schema_issue({"title": "T", "description": "D", "keywords": [keyword]})


def test_preview_keeps_image_edges_and_has_high_dpi_pixels(app_module):
    record = object.__new__(app_module.CardRecord)
    with Image.new("RGB", (600, 200), "green") as image:
        image.paste("red", (0, 0, 100, 200))
        image.paste("blue", (500, 0, 600, 200))
        thumb = record._make_square_thumb(image)
    assert thumb.size == (384, 384)
    assert thumb.getpixel((0, 192)) == (255, 0, 0)
    assert thumb.getpixel((383, 192)) == (0, 0, 255)


def test_api_image_matches_exif_display_orientation(app_module):
    image = Image.new("RGB", (60, 20), "red")
    exif = Image.Exif()
    exif[274] = 6
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    with Image.open(io.BytesIO(app_module.prepare_image(buffer.getvalue()))) as encoded:
        assert encoded.size == (20, 60)


def test_background_generation_uses_captured_mode_without_reading_tk(app_module):
    app = _bare_app(app_module)
    def forbidden():
        raise AssertionError("Worker must not read Tk variables")
    app.mode_var = SimpleNamespace(get=forbidden)
    app._fallback_model_candidates = lambda name: []
    captured = []
    app._call_api_once = lambda *args, **kwargs: captured.append(kwargs["prompt"]) or {"title": "T"}
    result = app._call_api("image", "OpenAI GPT-5.5", "synthetic", mode=app_module.PROMPT_MODES[1])
    assert result == {"title": "T"}
    assert captured == [app_module.build_prompt(app_module.PROMPT_MODES[1])]


def test_background_logging_never_reads_tk_key_variable(app_module):
    app = _bare_app(app_module)
    app.key_var = SimpleNamespace(get=lambda: pytest.fail("Tk key read from worker"))
    results = []
    worker = threading.Thread(target=lambda: results.append(app._redact_secrets("synthetic-secret", ["synthetic-secret"])))
    worker.start()
    worker.join(2)
    assert results and "synthetic-secret" not in results[0]


def test_cancel_interrupts_real_retry_wait(app_module):
    app = _bare_app(app_module)
    app._stop_event = threading.Event()
    app.stop_flag = False
    started = threading.Event()
    def wait():
        started.set()
        app._wait_for_retry(30)
    worker = threading.Thread(target=wait)
    worker.start()
    assert started.wait(1)
    app._stop()
    worker.join(1)
    assert not worker.is_alive()


def test_minimax_native_request_disables_default_reasoning_for_metadata(app_module):
    app = _bare_app(app_module)
    payloads = []
    response = SimpleNamespace(status_code=200, json=lambda: {"choices": [{"message": {"content": "{}"}}]})
    session = SimpleNamespace(post=lambda url, **kwargs: payloads.append(kwargs["json"]) or response)
    app._openai_compat_request(session, "MiniMax M3", "synthetic", "JSON", "image", "https://api.minimaxi.com/v1/chat/completions", 256)
    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert payloads[0]["reasoning_split"] is True


@pytest.mark.parametrize("url,expected", [
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", False),
    ("https://relay.example.test/v1", None),
])
def test_qwen_thinking_option_is_scoped_to_official_endpoint(app_module, url, expected):
    app = _bare_app(app_module)
    payloads = []
    response = SimpleNamespace(status_code=200, json=lambda: {"choices": [{"message": {"content": "{}"}}]})
    session = SimpleNamespace(post=lambda url, **kwargs: payloads.append(kwargs["json"]) or response)
    app._openai_compat_request(session, "Qwen 3.7 Plus", "synthetic", "JSON", "image", url, 2048)
    assert payloads[0].get("enable_thinking") is expected


def test_metadata_correction_repairs_long_title_without_overwriting_valid_description(app_module):
    app = _bare_app(app_module)
    description = (
        "Mountains rise above a lake with trees along the water and clouds overhead. "
        "Evening light falls across the landscape, showing the rocky slopes and the still water in a wide view."
    )
    raw = {"title": "A very long title with more words than the stock metadata title policy allows",
           "description": description, "keywords": ["mountain", "lake", "clouds", "trees", "evening"]}
    calls = []
    def repair(*args, **kwargs):
        calls.append(kwargs["prompt"])
        return {"title": "Mountain and Lake Landscape beneath Clouds in Evening Light",
                "description": "An invalid replacement", "keywords": ["mountains", "forest"]}
    app._call_api_once = repair
    app._repair_api_call = lambda *args: pytest.fail("Must not spend on a second correction request")
    result = app._apply_sanitizer_and_repair(raw, "image", {"type": "cloud"}, "synthetic", "", "", "Qwen 3.7 Plus")
    assert len(calls) == 1
    assert result["description"] == description
    assert result["title"] == "Mountain and Lake Landscape beneath Clouds in Evening Light"
    assert "mountains" not in result["keywords"] and "forest" in result["keywords"]
    assert not any(issue.startswith(("Title", "Description")) for issue in app_module.validate_stock_metadata(
        result["title"], result["description"], result["keywords"]
    ))


@pytest.mark.parametrize("cancelled", [False, True])
def test_bad_or_cancelled_metadata_correction_preserves_visible_review(app_module, cancelled):
    app = _bare_app(app_module)
    app.stop_flag = cancelled
    calls = []
    app._call_api_once = lambda *args, **kwargs: calls.append(True) or {"title": "Still short", "description": "Still short", "keywords": []}
    app._repair_api_call = lambda *args: pytest.fail("Must not stack another repair")
    raw = {"title": "Short", "description": "Short", "keywords": ["mountain"]}
    result = app._apply_sanitizer_and_repair(raw, "image", {"type": "cloud"}, "synthetic", "", "", "Qwen 3.7 Plus")
    assert result["title"] == raw["title"] and result["description"] == raw["description"]
    assert len(calls) == (0 if cancelled else 1)
    assert any(issue.startswith("Title") for issue in app_module.validate_stock_metadata(
        result["title"], result["description"], result["keywords"]
    ))


def test_repeated_regeneration_keeps_manual_fields_and_undo_restores_their_state(app_module, monkeypatch):
    monkeypatch.setattr(app_module.tk, "BooleanVar", lambda **kwargs: SimpleNamespace(set=lambda value: None))
    app = SimpleNamespace(mode_var=SimpleNamespace(get=lambda: app_module.PROMPT_MODES[0]),
                          model_var=SimpleNamespace(get=lambda: "Qwen 3.7 Plus"), data_cache={})
    record = app_module.CardRecord("synthetic.jpg", app)
    record._cached_data = {"title": "My deliberately edited title", "description": "Original description", "keywords": ["mountain"]}
    record.manual_fields = {"title"}
    record.manual_modified = True
    record.last_generated_at = "original-time"
    record.last_generated_model = "original-model"
    generated = {"title": "A model generated replacement title", "description": "New description", "keywords": ["lake"]}
    for _ in range(2):
        record.regen_scope = "unmodified"
        record.fill(generated)
        assert record._cached_data["title"] == "My deliberately edited title"
        assert record.manual_fields == {"title"}
        assert record.manual_modified
        assert app.data_cache.get(record.path) == record._cached_data
    assert record.undo_last_generation()
    assert record.undo_last_generation()
    assert record.manual_fields == {"title"} and record.manual_modified
    assert record.last_generated_at == "original-time"
    assert record.last_generated_model == "original-model"
    assert app.data_cache[record.path] == record._cached_data


def test_usable_keywords_are_not_padded_by_an_extra_paid_request(app_module):
    app = _bare_app(app_module)
    app._repair_api_call = lambda *args: pytest.fail("Do not request filler to meet a recommended count")
    app._call_api_once = lambda *args, **kwargs: pytest.fail("No content defects need correction")
    raw = {
        "title": "Traditional Chinese opera performer with colorful face masks",
        "description": "An illustration of a Chinese opera performer in an ornate costume. Painted masks surround the central figure, with red lanterns and embroidered golden dragons visible across the stage and flowing robes.",
        "keywords": ["chinese opera", "opera mask", "face paint", "headdress", "traditional costume", "red lantern"],
    }
    result = app._apply_sanitizer_and_repair(raw, "image", {"type": "cloud"}, "synthetic", "", "", "MiniMax M3")
    assert set(result["keywords"]) == set(raw["keywords"])
    _, review = app_module.classify_stock_metadata(result["title"], result["description"], result["keywords"])
    assert review == []  # Six concrete terms do not need filler to meet a quota.


def test_official_model_permission_error_and_gateway_html_have_actionable_feedback(app_module):
    app = _bare_app(app_module)
    message = app._categorize_http_error("Moonshot Kimi", 404,
        '{"error":{"message":"Not found the model kimi-k2.5 or Permission denied"}}',
        "https://api.moonshot.cn/v1/chat/completions")
    assert "model access permissions" in message
    assert "relay route" not in message
    message = app._categorize_http_error("OpenAI GPT-5.5", 524, "<!DOCTYPE html><html>gateway failed</html>", "https://relay.example/v1/responses")
    assert "524" in message and "gateway" in message
    assert "<html>" not in message
