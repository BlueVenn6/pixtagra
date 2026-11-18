"""Cancellation must retain paid results and recovery must survive process exit."""
import threading
import pytest
from types import SimpleNamespace
from PIL import Image

from tests.runtime.test_stage2_isolation import app_module, _bare_app


def test_cancel_drains_started_requests_without_starting_remaining_images(app_module):
    app = _bare_app(app_module)
    app.stop_flag = False
    started = []
    delivered = []
    barrier = threading.Barrier(2)
    cards = list(range(20))
    def process(card, *args):
        started.append(card)
        barrier.wait(timeout=3)
        app.stop_flag = True
        return {"title": str(card)}
    app._process = process
    app._queue_analysis_result = lambda card, result, *args: delivered.append((card, result))
    app._queue_analysis_finished = lambda: delivered.append("finished")
    app._analyze_thread(cards, "OpenAI GPT-5.5", "mode", None, "", "")
    assert sorted(started) == [0, 1]
    assert sorted(item[0] for item in delivered if item != "finished") == [0, 1]
    assert delivered[-1] == "finished"


def test_retry_incomplete_review_reuses_generated_text(app_module, tmp_path):
    app = _bare_app(app_module)
    app.stop_flag = False
    app._batch_visual_review_enabled = True
    path = tmp_path / "test.png"
    Image.new("RGB", (100, 100)).save(path)
    existing = {"title": "Mountain and Lake Landscape beneath Clouds in Evening Light",
                "description": "Mountains rise above a lake with trees along the water and clouds overhead. Evening light falls across the landscape, showing the rocky slopes and the still water in a wide view.",
                "keywords": ["mountain", "lake", "trees", "clouds", "evening"],
                "keyword_review": {"status": "pending", "stage": "review"}}
    card = SimpleNamespace(path=str(path), _resume_data=existing, removed=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("A review retry must not regenerate or repair existing content")
    app._call_api = forbidden
    app._apply_sanitizer_and_repair = forbidden
    calls = []
    app._review_generated_keywords = lambda data, *args: calls.append(data) or data
    result = app._process(card, "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "")
    assert result == existing
    assert len(calls) == 1


def test_cancel_prevents_parameter_negotiation_from_issuing_second_paid_request(app_module):
    app = _bare_app(app_module)
    app.stop_flag = False
    app.is_processing = True
    calls = []
    def post(*args, **kwargs):
        calls.append(args)
        app.stop_flag = True
        return SimpleNamespace(status_code=400, text='{"error":{"message":"enable_thinking is unsupported"}}')
    with pytest.raises(app_module._FatalApiError, match="cancelled"):
        app._openai_compat_request(SimpleNamespace(post=post), "Qwen 3.7 Plus", "synthetic", "prompt", "image",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", 2048)
    assert len(calls) == 1


def test_cancel_during_review_retains_the_review_that_already_returned(app_module):
    app = _bare_app(app_module)
    app.stop_flag = False
    def request(*args, **kwargs):
        app.stop_flag = True
        return {"checks": [{"id": 0, "verdict": "supported", "reason": "A mask is visible"}]}
    app._call_api_once = request
    result = app._review_generated_keywords({"keywords": ["mask"]}, "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert result["keyword_review"]["status"] == "reviewed"


def test_review_retry_updates_manual_keyword_verdict_without_discarding_user_terms(app_module, monkeypatch):
    monkeypatch.setattr(app_module.tk, "BooleanVar", lambda **kwargs: SimpleNamespace(set=lambda value: None))
    app = SimpleNamespace(data_cache={}, mode_var=SimpleNamespace(get=lambda: app_module.PROMPT_MODES[0]),
                          model_var=SimpleNamespace(get=lambda: "Qwen 3.7 Plus"))
    card = app_module.CardRecord("synthetic.png", app)
    card._cached_data = {"title": "Existing title", "description": "Existing description",
                        "keywords": ["mask", "snow"], "keyword_review": {"status": "unavailable"}}
    card.manual_fields = {"keywords"}
    card.regen_scope = "resume"
    reviewed = app_module.apply_keyword_review(card._cached_data, {"checks": [
        {"id": 0, "verdict": "supported", "reason": "Mask is visible"},
        {"id": 1, "verdict": "unsupported", "reason": "No snow is visible"}]}, "Qwen 3.7 Plus")
    card.fill(reviewed)
    assert set(card._cached_data["keywords"]) == {"mask", "snow"}
    assert card._cached_data["keyword_review"]["status"] == "reviewed"
    assert any("No snow is visible" in issue for issue in card.quality_issues)
