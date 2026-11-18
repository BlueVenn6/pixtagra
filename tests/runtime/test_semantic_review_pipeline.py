"""Fact audit and focused dedup must both succeed before a review is complete."""
import json
import threading
import pytest
from tests.runtime.test_stage2_isolation import app_module, _bare_app
from tests.runtime.test_export_delivery import metadata
from pixtagra_quality import build_keyword_review_prompt, build_keyword_deduplication_prompt
from pixtagra_quality import apply_keyword_review
from tests.runtime.test_keyword_quality_policy import generated
from types import SimpleNamespace
from PIL import Image


def fixture_data():
    return {**metadata(), "keyword_policy_version": 3,
            "keywords": ["absent utensil", "cooked meal", "metal fork", "fork", "yellow sauce", "thick sauce"]}


def fact_response():
    return {"visible_facts": ["A meal is visible", "A fork is beside it"],
            "checks": [{"id": i, "verdict": "unsupported" if i == 0 else "supported", "reason": "Not present" if i == 0 else "Directly visible"} for i in range(6)],
            "caption_checks": [{"field": field, "verdict": "supported", "reason": "Visible scene"} for field in ("title", "description")], "duplicate_groups": []}


def dedup_response():
    return {"duplicate_groups": [{"keep": 1, "remove": [2], "reason": "Same utensil search intent"}]}


def test_production_review_maps_focused_dedup_after_factual_removals(app_module):
    app = _bare_app(app_module); calls = []
    def call(*args, **kwargs):
        calls.append(kwargs["response_kind"])
        return dedup_response() if kwargs["response_kind"] == "keyword_deduplication" else fact_response()
    app._call_api_once = call
    result = app._review_generated_keywords(fixture_data(), "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert result["keywords"] == ["cooked meal", "metal fork", "yellow sauce", "thick sauce"]
    assert calls == ["keyword_review", "keyword_deduplication"]
    by_word = {c["keyword"]: c for c in result["keyword_review"]["checks"]}
    assert by_word["absent utensil"]["reason"] == "Not present"
    assert by_word["fork"]["verdict"] == "unsupported"
    assert by_word["yellow sauce"]["verdict"] == by_word["thick sauce"]["verdict"] == "supported"
    assert result["keyword_review"]["reviewed_keywords"] == result["keywords"]


def test_secondary_review_retries_transient_relay_error(app_module):
    app = _bare_app(app_module); app.error_lock = threading.Lock(); app.api_errors = []
    calls = []; delays = []
    app._wait_for_retry = lambda seconds: delays.append(seconds)

    def call(*args, **kwargs):
        kind = kwargs["response_kind"]
        calls.append(kind)
        if kind == "keyword_review" and calls.count("keyword_review") == 1:
            raise app_module._RetryableApiError("synthetic relay timeout", kind="http_524", retry_after=0)
        return dedup_response() if kind == "keyword_deduplication" else fact_response()

    app._call_api_once = call
    result = app._review_generated_keywords(fixture_data(), "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert result["keyword_review"]["status"] == "reviewed"
    assert calls == ["keyword_review", "keyword_review", "keyword_deduplication"]
    assert delays == [0]


def test_secondary_review_switches_to_compact_prompt_after_transient_relay_error(app_module):
    app = _bare_app(app_module); app.error_lock = threading.Lock(); app.api_errors = []
    prompts = []
    app._wait_for_retry = lambda seconds: None

    def call(*args, **kwargs):
        prompts.append(kwargs["prompt"])
        if len(prompts) == 1:
            raise app_module._RetryableApiError("synthetic relay timeout", kind="http_524", retry_after=0)
        return fact_response()

    app._call_api_once = call
    data = {**fixture_data(), "keyword_policy_version": 2}
    result = app._review_generated_keywords(data, "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert result["keyword_review"]["status"] == "reviewed"
    assert len(prompts) == 2
    assert len(prompts[1]) < len(prompts[0])
    assert "compact JSON" in prompts[1]


@pytest.mark.parametrize("malformed", ["duplicate_id", "missing_id"])
def test_incomplete_factual_audit_is_re_requested_and_never_normalized_to_success(app_module, malformed):
    app = _bare_app(app_module); app.error_lock = threading.Lock(); app.api_errors = []
    prompts = []; app._wait_for_retry = lambda seconds: None
    def call(*args, **kwargs):
        if kwargs["response_kind"] == "keyword_deduplication": return dedup_response()
        prompts.append(kwargs["prompt"])
        response = fact_response()
        if len(prompts) == 1:
            if malformed == "duplicate_id": response["checks"].append(response["checks"][0].copy())
            else: response["checks"].pop()
        return response
    app._call_api_once = call
    result = app._review_generated_keywords(fixture_data(), "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert result["keyword_review"]["status"] == "reviewed"
    assert len(prompts) == 2
    assert "compact JSON" in prompts[1]
    assert len(result["keyword_review"]["checks"]) == len(fixture_data()["keywords"])
    assert not app.api_errors


def test_repeated_invalid_factual_audit_exhausts_same_finite_attempt_budget(app_module):
    app = _bare_app(app_module); app.error_lock = threading.Lock(); app.api_errors = []
    calls = []; app._wait_for_retry = lambda seconds: None
    def call(*args, **kwargs):
        calls.append(kwargs["response_kind"])
        response = fact_response(); response["checks"].pop()
        return response
    app._call_api_once = call
    data = fixture_data()
    result = app._review_generated_keywords(data, "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert calls == ["keyword_review"] * 3
    assert result["keyword_review"]["status"] == "unavailable"
    assert result["keywords"] == data["keywords"]
    assert app.api_errors


def test_secondary_review_stops_after_bounded_transient_retries(app_module):
    app = _bare_app(app_module); app.error_lock = threading.Lock(); app.api_errors = []
    calls = []; delays = []
    app._wait_for_retry = lambda seconds: delays.append(seconds)

    def call(*args, **kwargs):
        calls.append(kwargs["response_kind"])
        raise app_module._RetryableApiError("synthetic relay timeout", kind="http_524", retry_after=0)

    app._call_api_once = call
    result = app._review_generated_keywords(fixture_data(), "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert result["keyword_review"]["status"] == "unavailable"
    assert calls == ["keyword_review", "keyword_review", "keyword_review"]
    assert delays == [0, 0]
    assert app.api_errors


def test_semantic_groups_cannot_silently_drop_an_unmentioned_keyword(app_module):
    app = _bare_app(app_module); app.error_lock = threading.Lock(); app.api_errors = []
    def call(*args, **kwargs):
        if kwargs["response_kind"] != "keyword_deduplication": return fact_response()
        # The old redundant retained list omitted words not removed by a group.
        return {"duplicate_groups": dedup_response()["duplicate_groups"], "keep_ids": [1]}
    app._call_api_once = call
    result = app._review_generated_keywords(fixture_data(), "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert result["keyword_review"]["status"] == "reviewed"
    assert result["keywords"] == ["cooked meal", "metal fork", "yellow sauce", "thick sauce"]


@pytest.mark.parametrize("failure", ["network", "missing_groups", "invented_id", "self_removed"])
def test_focused_dedup_failure_never_claims_a_complete_review(app_module, failure):
    app = _bare_app(app_module); app.error_lock = threading.Lock(); app.api_errors = []
    def call(*args, **kwargs):
        if kwargs["response_kind"] != "keyword_deduplication": return fact_response()
        if failure == "network": raise RuntimeError("Synthetic unavailable response")
        result = dedup_response()
        if failure == "missing_groups": result.pop("duplicate_groups")
        if failure == "invented_id": result["duplicate_groups"][0]["remove"] = [90]
        if failure == "self_removed": result["duplicate_groups"][0]["remove"] = [1]
        return result
    app._call_api_once = call
    data = fixture_data()
    result = app._review_generated_keywords(data, "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert result["keyword_review"]["status"] == "unavailable"
    assert result["keywords"] == data["keywords"]
    assert app.api_errors


def test_historical_generation_keeps_its_one_request_review_path(app_module):
    app = _bare_app(app_module); calls = []
    def call(*args, **kwargs): calls.append(kwargs["response_kind"]); return fact_response()
    app._call_api_once = call
    data = {**fixture_data(), "keyword_policy_version": 2}
    result = app._review_generated_keywords(data, "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert result["keyword_review"]["status"] == "reviewed" and calls == ["keyword_review"]


def test_fact_only_prompt_does_not_request_a_second_semantic_judgment():
    prompt = build_keyword_review_prompt(["fork", "metal fork"], {"title": "A fork"}, deduplicate=False)
    example, _ = json.JSONDecoder().raw_decode(prompt.split("schema: ", 1)[1])
    assert "duplicate_groups" not in example
    assert {c["field"] for c in example["caption_checks"]} == {"title", "description"}
    assert "Semantic deduplication is audited separately" in prompt


def test_semantic_protocol_has_one_authoritative_set_of_deletions():
    prompt = build_keyword_deduplication_prompt(["fork", "metal fork"])
    example, _ = json.JSONDecoder().raw_decode(prompt.split("Return ONLY JSON: ", 1)[1])
    assert set(example) == {"duplicate_groups"}
    assert "Any id not explicitly removed is retained unchanged" in prompt
    assert "Related is not duplicate" in prompt
    assert "Do not pursue a numeric keyword quota" in prompt
    assert "Different independent facets" in prompt


def test_semantic_shortfall_triggers_the_single_production_correction(app_module, tmp_path):
    app = _bare_app(app_module)
    app.error_lock = threading.Lock(); app.api_errors = []
    app._batch_visual_review_enabled = True
    app._checkpoint_result = lambda *args: None
    app._call_api = lambda *args, **kwargs: generated(24)
    calls = []; rounds = {"keyword_review": 0, "keyword_deduplication": 0}
    def call(*args, **kwargs):
        kind = kwargs.get("response_kind"); calls.append(kind)
        if kind not in rounds: return generated(26)
        rounds[kind] += 1
        if kind == "keyword_deduplication":
            return ({"duplicate_groups": [{"keep": 0, "remove": list(range(19, 24)), "reason": "Synthetic equivalent intents"}]}
                    if rounds[kind] == 1 else {"duplicate_groups": []})
        count = 24 if rounds[kind] == 1 else 26
        return {"visible_facts": ["A landscape is visible", "Water and vegetation are visible"],
                "checks": [{"id": i, "verdict": "supported", "reason": "Visible fixture concept"} for i in range(count)],
                "caption_checks": [{"field": field, "verdict": "supported", "reason": "Visible fixture scene"}
                                   for field in ("title", "description")]}
    app._call_api_once = call
    path = tmp_path / "fixture.png"; Image.new("RGB", (80, 60)).save(path)
    result = app._process(SimpleNamespace(path=str(path)), "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "")
    assert calls == ["keyword_review", "keyword_deduplication", None, "keyword_review", "keyword_deduplication"]
    assert result["keyword_review"]["correction"]["before_count"] == 19
    assert len(result["keywords"]) == 26
    assert result["keyword_review"]["semantic_review"]["retained_count"] == 26


def test_cancel_after_fact_audit_sends_no_semantic_request(app_module):
    app = _bare_app(app_module); calls = []
    def call(*args, **kwargs):
        calls.append(kwargs["response_kind"]); app.stop_flag = True; return fact_response()
    app._call_api_once = call
    result = app._review_generated_keywords(fixture_data(), "image", "Qwen 3.7 Plus", "synthetic", "", {"type": "cloud"})
    assert calls == ["keyword_review"] and result["keyword_review"]["status"] == "cancelled"


def test_removed_uncertainty_does_not_force_an_unneeded_correction(app_module):
    app = _bare_app(app_module)
    data = {**generated(21), "keyword_policy_version": 3}
    data = apply_keyword_review(data, {"visible_facts": ["A landscape is visible", "Distinct objects are visible"],
        "duplicate_groups": [], "checks": [{"id": i, "verdict": "uncertain" if i == 0 else "supported", "reason": "Fixture evidence"}
                                          for i in range(21)]}, "Qwen 3.7 Plus")
    assert len(data["keywords"]) == 20
    app._call_api_once = lambda *a, **kw: pytest.fail("Qualified supported terms must not trigger paid padding")
    assert app._correct_reviewed_metadata(data, "image", "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "synthetic", "", {"type": "cloud"}) is data
