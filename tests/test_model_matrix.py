from scripts.write_model_compatibility_matrix import build_matrix


def record(phase, count, image="opera.png"):
    return {"model": "OpenAI GPT-5.5", "phase": phase, "image": image,
            "status": "returned_metadata", "result": {"keywords": ["tag"] * count},
            "raw": "SECRET_MUST_NOT_BE_COPIED", "api_key": "SECRET_MUST_NOT_BE_COPIED"}


def test_review_fixture_does_not_replace_generated_keyword_count_or_drop_second_image():
    matrix = build_matrix({"records": [record("metadata", 35), record("review", 7),
                                      record("metadata:portrait", 29, "portrait.png")]}, [])
    row = next(r for r in matrix["models"] if r["model"] == "OpenAI GPT-5.5")
    assert [r["keyword_count"] for r in row["generation_runs"]] == [35, 29]
    assert row["review_runs"][0]["keyword_count"] == 7
    assert row["review_runs"][0]["benchmark"] is None
    assert "SECRET_MUST_NOT_BE_COPIED" not in str(matrix)


def test_models_without_live_evidence_remain_untested():
    matrix = build_matrix({"records": []}, [])
    assert matrix["models"]
    assert all(not r["generation_runs"] and not r["review_runs"] for r in matrix["models"])


def test_legacy_error_strings_are_counted_without_disclosing_their_content():
    sample = record("metadata", 35)
    sample["api_errors"] = ["SECRET_ERROR_TEXT"]
    matrix = build_matrix({"records": [sample]}, [])
    row = next(r for r in matrix["models"] if r["model"] == "OpenAI GPT-5.5")
    assert row["generation_runs"][0]["api_error_count"] == 1
    assert row["generation_runs"][0]["error_code"] is None
    assert "SECRET_ERROR_TEXT" not in str(matrix)
