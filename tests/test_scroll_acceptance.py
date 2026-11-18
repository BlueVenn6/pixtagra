"""Do not treat functional-only or incomplete GUI evidence as release acceptance."""
import pytest

from pixtagra_workspace_probe import SCROLL_BUDGET_MS, assess_scroll_performance, p95


def acceptable_result():
    return {**{name: limit for name, limit in SCROLL_BUDGET_MS.items()},
            "loaded": 500, "sample_images": [f"photo-{i}.jpg" for i in range(500)],
            "keywords_per_record": 35, "wheel_moves": 90, "scroll_cycles": 5,
            "passed": True, "screenshot_error": None}


def test_functional_pass_cannot_hide_scroll_latency_or_an_unmeasured_metric():
    result = acceptable_result()
    assert assess_scroll_performance(result)["passed"]
    for name in SCROLL_BUDGET_MS:
        for invalid in (SCROLL_BUDGET_MS[name] + .01, None, float("nan"), -1):
            assert name in assess_scroll_performance({**result, name: invalid})["failures"]


@pytest.mark.parametrize("change", [
    {"loaded": 499}, {"sample_images": ["same-photo.jpg"] * 500},
    {"keywords_per_record": 3}, {"wheel_moves": 89}, {"scroll_cycles": 4},
    {"passed": False}, {"screenshot_error": "capture failed"},
])
def test_incomplete_or_failed_evidence_cannot_authorize_release(change):
    assert not assess_scroll_performance({**acceptable_result(), **change})["passed"]


def test_small_sample_p95_uses_nearest_rank_including_the_tail():
    assert p95(list(range(1, 31))) == 29
