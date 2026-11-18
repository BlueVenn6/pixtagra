from pixtagra_evaluation import score_review


def test_score_counts_false_removals_and_missed_hallucinations_separately():
    case = {"id": "test", "supported": ["sand", "water"], "unsupported": ["stone wall"],
            "caption_verdicts": {"title": "unsupported"}}
    result = {"keyword_review": {"status": "reviewed", "checks": [
        {"keyword": "sand", "verdict": "unsupported"}, {"keyword": "water", "verdict": "supported"},
        {"keyword": "stone wall", "verdict": "supported"}],
        "caption_checks": [{"field": "title", "verdict": "supported"}]}}
    score = score_review(case, result)
    assert score["supported_recall"] == .5
    assert score["error_detection_rate"] == 0
    assert score["caption_errors"] == ["title"]
    assert not score["passed"]


def test_incomplete_review_never_scores_as_success():
    assert not score_review({"id": "test"}, {"keyword_review": {"status": "unavailable"}})["passed"]
