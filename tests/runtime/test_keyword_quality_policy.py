"""New-generation quality boundaries without truncating old or manual work."""
from types import SimpleNamespace

import pytest

from tests.runtime.test_stage2_isolation import app_module, _bare_app, _Var
from tests.runtime.test_export_delivery import metadata
from tests.test_language_support import chinese_metadata
from pixtagra_language import metadata_options
from pixtagra_keywords import validate_stock_metadata


WORDS = ["mountain", "lake", "forest", "water", "clouds", "sky", "river", "tree", "rock", "snow",
         "sunlight", "reflection", "shore", "valley", "pine", "meadow", "cliff", "stream", "grass", "peak",
         "path", "horizon", "island", "boulder", "ridge", "branch", "sand", "moss", "fern", "hill",
         "leaf", "stone", "rain", "fog", "outdoors", "sunset"]


DISTINCT_CLOUD_FACETS = ["cloud formations", "cloud texture", "cloud shading", "cloud edges", "cloud highlights"]


def test_current_policy_does_not_delete_distinct_facets_just_for_sharing_a_subject():
    from pixtagra_keywords import sanitize_keywords_for_stock
    kept, rejected, _ = sanitize_keywords_for_stock(DISTINCT_CLOUD_FACETS, keyword_policy_version=3)
    assert kept == DISTINCT_CLOUD_FACETS
    assert not rejected
    legacy, _, _ = sanitize_keywords_for_stock(DISTINCT_CLOUD_FACETS)
    assert legacy == DISTINCT_CLOUD_FACETS[:3]
    variants, rejected, _ = sanitize_keywords_for_stock(["cloud texture", "cloud textures", "texture cloud"], keyword_policy_version=3)
    assert variants == ["cloud texture"] and len(rejected) == 2


def test_current_subject_facets_survive_the_production_record_fill_path(app_module, monkeypatch):
    monkeypatch.setattr(app_module.tk, "BooleanVar", lambda **kw: _Var(True))
    app = SimpleNamespace(mode_var=_Var(app_module.PROMPT_MODES[1]), model_var=_Var("Qwen 3.7 Plus"), data_cache={})
    record = app_module.CardRecord("fixture.jpg", app)
    record.fill({**generated(15), "keywords": [*WORDS[:15], *DISTINCT_CLOUD_FACETS], "keyword_policy_version": 3})
    assert len(record.get_data(sync=False)[2]) == 20
    assert set(DISTINCT_CLOUD_FACETS) <= set(record.get_data(sync=False)[2])
    assert app.data_cache[record.path]["keywords"] == record.get_data(sync=False)[2]
    assert not any(issue.startswith("Only ") for issue in record.quality_issues)


def generated(count):
    return {**metadata(), "keywords": WORDS[:count], "keyword_policy_version": 2}


@pytest.mark.parametrize("count,valid", [(9, False), (10, True), (25, True), (26, True), (35, True), (36, False)])
def test_generated_boundary_uses_actual_retained_count(count, valid):
    data = generated(count)
    issues = validate_stock_metadata(data["title"], data["description"], data["keywords"], **metadata_options(data))
    assert (not issues) == valid


@pytest.mark.parametrize("count", [12, 26, 35])
def test_accepted_interval_never_calls_repair_for_count(app_module, count):
    app = _bare_app(app_module)
    app._repair_api_call = lambda *a, **kw: pytest.fail("No numeric padding inside the acceptance interval")
    data = generated(count)
    assert app._apply_sanitizer_and_repair(data, "image", {"type": "cloud"}, "synthetic", "", "", "Qwen 3.7 Plus")["keywords"] == WORDS[:count]


def test_below_ten_requests_only_gap_once_and_never_invents(app_module):
    app = _bare_app(app_module)
    calls = []
    def repair(*args, **kwargs):
        calls.append(args[3])
        return ["mountains", "lake"]  # Both duplicate retained subjects.
    app._repair_api_call = repair
    result = app._apply_sanitizer_and_repair(generated(9), "image", {"type": "cloud"}, "synthetic", "", "", "Qwen 3.7 Plus")
    assert calls == [1] and len(result["keywords"]) == 9
    assert any(issue.startswith("Only 9") for issue in validate_stock_metadata(result["title"], result["description"], result["keywords"], **metadata_options(result)))


def test_overflow_preserves_subject_order_and_pushes_use_cases_out(app_module):
    app = _bare_app(app_module)
    data = generated(36)
    data["keywords"] = ["poster background", *data["keywords"]]
    result = app._apply_sanitizer_and_repair(data, "", {"type": "local"}, "", "", "")
    assert result["keywords"] == WORDS[:35]


def test_legacy_record_and_manually_preserved_keywords_keep_35(app_module, monkeypatch):
    monkeypatch.setattr(app_module.tk, "BooleanVar", lambda **kw: _Var(True))
    app = SimpleNamespace(mode_var=_Var(app_module.PROMPT_MODES[0]), model_var=_Var("Qwen 3.7 Plus"), data_cache={})
    record = app_module.CardRecord("fixture.jpg", app)
    record.fill({**metadata(), "keywords": WORDS[:35]})
    assert record.get_data(sync=False)[2] == WORDS[:35]
    record.manual_fields = {"keywords"}
    record.regen_scope = "unmodified"
    record.fill(generated(10))
    assert record.get_data(sync=False)[2] == WORDS[:35]
    assert record._cached_data.get("keyword_policy_version") != 2


def test_visual_review_removals_recheck_minimum_without_second_fill(app_module):
    from pixtagra_quality import apply_keyword_review
    data = generated(10)
    checked = apply_keyword_review(data, {"checks": [
        {"id": i, "verdict": "unsupported" if i == 9 else "supported", "reason": "synthetic fixture evidence"}
        for i in range(10)]}, "Qwen 3.7 Plus")
    assert len(checked["keywords"]) == 9 and checked["keyword_policy_version"] == 2
    issues = validate_stock_metadata(checked["title"], checked["description"], checked["keywords"], **metadata_options(checked))
    assert any(issue.startswith("Only 9") for issue in issues)


def test_chinese_count_policy_and_session_marker_survive_round_trip():
    from pixtagra_session import metadata_only
    data = {**chinese_metadata(), "keyword_policy_version": 2}
    assert metadata_only(data)["keyword_policy_version"] == 2
    assert not validate_stock_metadata(data["title"], data["description"], data["keywords"], **metadata_options(data))


@pytest.mark.parametrize("count,valid", [(19, False), (20, True), (25, True), (30, True), (35, True), (36, False)])
def test_current_generation_requires_twenty_retained_terms(count, valid):
    data = {**generated(count), "keyword_policy_version": 3}
    issues = validate_stock_metadata(data["title"], data["description"], data["keywords"], **metadata_options(data))
    assert (not issues) == valid


def test_new_minimum_does_not_reinterpret_old_generated_or_manual_work():
    from pixtagra_keywords import keyword_limits
    from pixtagra_session import metadata_only
    assert keyword_limits(generated(12)) == (10, 35)
    assert keyword_limits({}) == (5, 49)
    data = {**generated(19), "keyword_policy_version": 3}
    assert keyword_limits(metadata_only(data)) == (20, 35)


@pytest.mark.parametrize("language,words", [("en", ["foreground", "background", "grass background", "close up"]),
                                          ("zh", ["前景", "背景", "草地背景", "近景特写"])])
def test_bare_frame_labels_cannot_count_toward_current_generation_minimum(language, words):
    from pixtagra_keywords import sanitize_keywords_for_stock
    kept, rejected, _ = sanitize_keywords_for_stock(words, language=language, keyword_policy_version=3)
    assert kept == words[2:] and len(rejected) == 2
    # Manual and historical records retain their established compatibility.
    assert sanitize_keywords_for_stock(words, language=language)[0] == words


def test_current_caption_repair_does_not_truncate_valid_candidates_to_minimum(app_module):
    app = _bare_app(app_module)
    data = {**generated(19), "keyword_policy_version": 3, "title": "Too short"}
    app._call_api_once = lambda *a, **kw: {**metadata(), "keywords": WORDS[:30]}
    result = app._apply_sanitizer_and_repair(
        data, "image", {"type": "cloud"}, "synthetic", "", "", "Qwen 3.7 Plus")
    assert result["keywords"] == WORDS[:30]


def test_current_keyword_repair_preserves_all_independent_returned_candidates(app_module):
    app = _bare_app(app_module)
    app._repair_api_call = lambda *a, **kw: WORDS[19:26]
    result = app._apply_sanitizer_and_repair(
        {**generated(19), "keyword_policy_version": 3}, "image", {"type": "cloud"},
        "synthetic", "", "", "Qwen 3.7 Plus")
    assert result["keywords"] == WORDS[:26]
