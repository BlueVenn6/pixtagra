"""Boundary tests for an isolated experiment, not proof of model accuracy."""
import copy
import json
from pathlib import Path

import pytest

from scripts.keyword_contract import apply_review, validate_candidate


def example(terms=("cattle", "livestock")):
    candidate = {"title": "A factual caption", "description": "A factual description", "relations": [],
                 "concepts": [{"id": f"c{i:02d}", "term": t, "entity": "e01", "kind": "subject",
                               "facet": "identity", "evidence_kind": "visible", "evidence": "image evidence",
                               "priority": i} for i, t in enumerate(terms, 1)]}
    review = {"checks": [{"id": n["id"], "verdict": "supported", "search": "useful", "reason": "visible"}
                         for n in candidate["concepts"]],
              "semantic_checked_ids": [n["id"] for n in candidate["concepts"]], "relations": [],
              "caption_checks": [{"field": f, "verdict": "supported", "reason": "visible"}
                                 for f in ("title", "description")]}
    return candidate, review


def test_reference_relations_are_not_equated_by_shared_subject():
    ref = json.loads((Path(__file__).parents[1] / "docs/quality/keyword-pilot-reference-v1.json").read_text(encoding="utf8"))
    for case in ref["cases"]:
        for row in case["relation_examples"]:
            if row["relation"] not in {"broader", "different_facet", "different_object", "equivalent_in_this_context"}:
                continue
            a, b = example((row["a"], row["b"]))
            kind = "equivalent" if row["action"] == "merge" else "broader" if row["relation"] == "broader" else "different_facet"
            b["relations"] = [{"a": "c01", "b": "c02", "type": kind, "reason": row["reason"]}]
            out = apply_review(a, b, "en")
            assert out["after_concept_review"] == (1 if row["action"] == "merge" else 2), row


@pytest.mark.parametrize("defect", ["missing_check", "duplicate_check", "unknown_id", "missing_semantic", "missing_caption", "illegal_delete"])
def test_incomplete_or_illegal_review_never_passes(defect):
    a, b = example()
    if defect == "missing_check": b["checks"].pop()
    if defect == "duplicate_check": b["checks"][1] = copy.deepcopy(b["checks"][0])
    if defect == "unknown_id": b["checks"][0]["id"] = "unknown"
    if defect == "missing_semantic": b["semantic_checked_ids"].pop()
    if defect == "missing_caption": b["caption_checks"].pop()
    if defect == "illegal_delete": b["relations"] = [{"a": "c01", "b": "c02", "type": "broader", "reason": "category", "remove": ["c02"]}]
    with pytest.raises(ValueError): apply_review(a, b, "en")


def test_unadjudicated_proposed_relation_is_fail_closed_for_manual_review():
    a, b = example(("cattle", "livestock"))
    a["relations"] = [{"a": "c01", "b": "c02", "type": "broader", "reason": "category"}]
    out = apply_review(a, b, "en")
    assert out["keywords"] == []
    assert out["unresolved_ids"] == ["c01", "c02"]
    assert out["unresolved_relationships"] == [{
        "a": "c01", "b": "c02", "reason": "review did not adjudicate candidate relation",
    }]
    assert not out["automatic_gate_only"]


def test_transitive_equivalence_cannot_hide_non_equivalence():
    a, b = example(("bicycle", "bike", "vehicle"))
    b["relations"] = [{"a": x, "b": y, "type": t, "reason": "relationship"} for x, y, t in
                      [("c01", "c02", "equivalent"), ("c02", "c03", "equivalent"), ("c01", "c03", "broader")]]
    with pytest.raises(ValueError, match="transitive"): apply_review(a, b, "en")


def test_uncertainty_is_not_counted_or_silently_accepted():
    a, b = example()
    b["relations"] = [{"a": "c01", "b": "c02", "type": "uncertain", "reason": "cannot resolve"}]
    out = apply_review(a, b, "en")
    assert out["keywords"] == [] and out["excluded_uncertain_ids"] == ["c01", "c02"]
    assert out["unresolved_ids"] == []
    assert not out["automatic_gate_only"]


def test_different_ids_do_not_prevent_merging_true_equivalents():
    a, b = example(("necktie", "tie"))
    b["relations"] = [{"a": "c01", "b": "c02", "type": "equivalent", "reason": "same accessory"}]
    assert apply_review(a, b, "en")["keywords"] == ["necktie"]


def test_twenty_candidate_ids_do_not_bypass_factual_or_count_gate():
    a, b = example(tuple(f"object {i}" for i in range(20)))
    b["checks"][0]["verdict"] = "unsupported"
    out = apply_review(a, b, "en")
    assert out["after_concept_review"] == 19
    assert not out["automatic_gate_only"]


def test_unknown_evidence_kind_fails_before_paid_review():
    a, _ = example()
    a["concepts"][0]["evidence_kind"] = "unsupported"
    with pytest.raises(ValueError, match="evidence kind"): validate_candidate(a, "en")


def test_candidate_order_is_authoritative_when_redundant_priority_is_omitted():
    a, _ = example(("cattle", "grass"))
    for node in a["concepts"]:
        node.pop("priority")
    validate_candidate(a, "en")
    assert [node["priority"] for node in a["concepts"]] == [1, 2]


def test_excluded_uncertain_candidate_does_not_poison_twenty_supported_terms():
    a, b = example(tuple("cat dog horse cow sheep goat pig duck hen goose tree leaf flower grass rock water cloud sky house table chair".split()))
    b["checks"][-1]["verdict"] = "uncertain"
    out = apply_review(a, b, "en")
    assert len(out["keywords"]) == 20
    assert out["excluded_uncertain_ids"] == ["c21"]
    assert out["automatic_gate_only"]
