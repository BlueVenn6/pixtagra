"""Production review must not leave a correctable shortfall unprocessed."""
import copy
import json
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from tests.runtime.test_stage2_isolation import app_module, _bare_app
from tests.runtime.test_keyword_quality_policy import generated, WORDS
from pixtagra_quality import apply_keyword_review


def reviewed(count=8):
    data = generated(count)
    return apply_keyword_review(data, {"checks": [
        {"id": i, "verdict": "supported", "reason": "Visible fixture subject"}
        for i in range(count)]}, "Qwen 3.7 Plus")


def test_production_flow_corrects_shortfall_only_after_first_review(app_module, tmp_path):
    app = _bare_app(app_module)
    app.error_lock = threading.Lock()
    app.api_errors = []
    app._batch_visual_review_enabled = True
    app._checkpoint_result = lambda *args: None
    app._call_api = lambda *args, **kwargs: generated(24)
    requests = []
    def request(*args, **kwargs):
        requests.append(kwargs)
        if kwargs.get("response_kind") == "keyword_deduplication":
            candidates = json.loads(kwargs["prompt"].split("CANDIDATES:\n", 1)[1])
            assert len(candidates) in (18, 26)
            return {"duplicate_groups": []}
        if kwargs.get("response_kind") != "keyword_review":
            return generated(26)
        count = 24 if len(requests) == 1 else 26
        return {"visible_facts": ["A landscape is visible", "Water and trees are visible"], "duplicate_groups": [],
                "checks": [{"id": i, "verdict": "unsupported" if count == 24 and i >= 18 else "supported",
                            "reason": "Distinct visible concept"} for i in range(count)],
                "caption_checks": [{"field": f, "verdict": "supported", "reason": "Visible scene"}
                                   for f in ("title", "description")]}
    app._call_api_once = request
    path = tmp_path / "fixture.png"
    Image.new("RGB", (80, 60)).save(path)
    result = app._process(SimpleNamespace(path=str(path)), "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "")
    assert [r.get("response_kind") for r in requests] == [
        "keyword_review", "keyword_deduplication", None, "keyword_review", "keyword_deduplication"]
    assert result["keyword_policy_version"] == 3
    assert len(result["keywords"]) == 26
    assert result["keyword_review"]["reviewed_keywords"] == result["keywords"]
    assert result["keyword_review"]["correction"]["before_count"] == 18
    assert result["keyword_review"]["correction"]["after_count"] == 26


@pytest.mark.parametrize("final_count", [8, 12])
def test_one_correction_bound_and_fresh_verdicts_even_if_still_short(app_module, final_count):
    app = _bare_app(app_module)
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("response_kind") != "keyword_review":
            return generated(12)
        return {"checks": [{"id": i, "verdict": "supported" if i < final_count else "unsupported",
                            "reason": "New image evidence"} for i in range(12)],
                "caption_checks": [{"field": f, "verdict": "supported", "reason": "Visible scene"}
                                   for f in ("title", "description")]}
    app._call_api_once = request
    result = app._correct_reviewed_metadata(reviewed(), "image", "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "", {"type": "cloud"})
    assert len(calls) == 2 and len(result["keywords"]) == final_count
    assert result["keyword_review"]["reviewed_keywords"] == WORDS[:final_count]
    assert app._correct_reviewed_metadata(result, "image", "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "", {"type": "cloud"}) == result
    assert len(calls) == 2


@pytest.mark.parametrize("condition", ["qualified", "legacy", "local", "cancelled", "unavailable"])
def test_no_extra_requests_for_protected_or_already_qualified_content(app_module, condition):
    app = _bare_app(app_module)
    data = reviewed(12 if condition == "qualified" else 8)
    if condition == "legacy":
        data.pop("keyword_policy_version")
    if condition == "cancelled":
        app.stop_flag = True
    if condition == "unavailable":
        data["keyword_review"]["status"] = "unavailable"
    app._call_api_once = lambda *a, **kw: pytest.fail("Unexpected paid correction")
    assert app._correct_reviewed_metadata(data, "image", "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "", {"type": "local" if condition == "local" else "cloud"}) is data


def test_failed_correction_preserves_original_review_and_shortfall(app_module):
    app = _bare_app(app_module)
    app.error_lock = threading.Lock()
    app.api_errors = []
    data = reviewed()
    original = copy.deepcopy(data)
    def fail(*args, **kwargs):
        raise ValueError("Invalid model JSON")
    app._call_api_once = fail
    result = app._correct_reviewed_metadata(data, "image", "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "", {"type": "cloud"})
    assert data == original and result["keywords"] == original["keywords"]
    assert result["keyword_review"]["correction"]["status"] == "failed"
    assert any("post-review correction failed" in error for error in app.api_errors)


@pytest.mark.parametrize("protected_fields", [("title", "description"), ("title",), ("description",)])
def test_keyword_correction_preserves_captions_already_accepted_by_image_review(app_module, protected_fields):
    app = _bare_app(app_module)
    data = generated(8)
    data = apply_keyword_review(data, {
        "checks": [{"id": i, "verdict": "supported", "reason": "Visible landscape detail"} for i in range(8)],
        "caption_checks": [{"field": field, "verdict": "supported" if field in protected_fields else "unsupported",
                            "reason": "Original visual audit"} for field in ("title", "description")],
    }, "Qwen 3.7 Plus", review_metadata=True)
    replacement = {**generated(12),
                   "title": "A Different Mountain Landscape With Snow and Distant Trees",
                   "description": "A different mountain landscape shows snow and distant trees beside a clear lake. Rocks and green vegetation line the shore, while soft daylight illuminates the water and the surrounding slopes beneath a partly cloudy sky."}
    reviewed_data = []
    app._call_api_once = lambda *args, **kwargs: replacement
    def review(candidate, *args):
        reviewed_data.append(copy.deepcopy(candidate))
        return candidate
    app._review_generated_keywords = review
    result = app._correct_reviewed_metadata(data, "image", "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "", {"type": "cloud"})
    assert reviewed_data, "The merged metadata must receive a fresh visual review"
    for field in ("title", "description"):
        expected = data[field] if field in protected_fields else replacement[field]
        assert result[field] == expected
        assert reviewed_data[0][field] == expected


def test_correction_merges_verified_anchors_instead_of_losing_them(app_module):
    app = _bare_app(app_module)
    data = {**generated(12), "keyword_policy_version": 3}
    decisions = [{"id": i, "verdict": "uncertain" if i == 0 else "unsupported" if i == 1 else "supported",
                  "reason": "Independent visual evidence"} for i in range(12)]
    data = apply_keyword_review(data, {"checks": decisions, "duplicate_groups": [],
                               "visible_facts": ["A landscape is visible", "Several distinct objects are visible"]}, "Qwen 3.7 Plus")
    app._call_api_once = lambda *args, **kwargs: {**generated(20), "keywords": WORDS[12:32]}
    received = []
    def review(candidate, *args):
        received.append(copy.deepcopy(candidate))
        return candidate
    app._review_generated_keywords = review
    result = app._correct_reviewed_metadata(data, "image", "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "", {"type": "cloud"})
    assert received, "Merged words must all receive a new image review"
    assert set(WORDS[2:12]) <= set(received[0]["keywords"]), "The correction omitted already verified concepts"
    assert set(result["keywords"]) == set(WORDS[2:32])
    assert WORDS[0] not in result["keywords"] and WORDS[1] not in result["keywords"]


def test_correction_retains_the_new_relevance_order_and_appends_missing_anchors(app_module):
    app = _bare_app(app_module)
    data = reviewed(8)
    preferred = [WORDS[12], WORDS[0], *WORDS[8:12]]
    app._call_api_once = lambda *args, **kwargs: {**generated(12), "keywords": preferred}
    app._review_generated_keywords = lambda candidate, *args: candidate
    result = app._correct_reviewed_metadata(data, "image", "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "", {"type": "cloud"})
    assert result["keywords"][:len(preferred)] == preferred
    assert set(WORDS[:8]) <= set(result["keywords"])
