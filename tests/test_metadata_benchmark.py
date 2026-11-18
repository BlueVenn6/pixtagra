import json
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).parent))
from test_llm_keyword_generation import _load_app_module


@pytest.fixture(scope="module")
def app_module():
    return _load_app_module()


@pytest.fixture(scope="module")
def benchmark_manifest():
    manifest_path = Path(__file__).parent / "fixtures" / "metadata_benchmark" / "manifest.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def test_real_image_metadata_benchmark(app_module, benchmark_manifest):
    """Run the same quality and compliance gates used by the export workflow."""
    fixture_dir = Path(__file__).parent / "fixtures" / "metadata_benchmark"
    results = []

    for record in benchmark_manifest["records"]:
        image_path = fixture_dir / record["file"]
        image_issue = app_module.validate_image(image_path)
        assert image_issue is None, f"{record['file']}: {image_issue}"
        ranked_keywords = app_module.rank_keywords_for_stock(record["keywords"], record["mode"])
        issues = app_module.validate_stock_metadata(
            record["title"], record["description"], ranked_keywords, record["mode"]
        )
        assert issues == [], f"{record['file']}: {issues}"

        warnings = app_module.metadata_compliance_warnings(
            record["title"], record["description"], ranked_keywords, record.get("compliance")
        )
        for expected in record.get("expected_warnings", []):
            assert any(expected in warning for warning in warnings), (
                f"{record['file']}: expected compliance warning {expected!r}, got {warnings!r}"
            )
        results.append({"file": record["file"], "quality": "pass", "warnings": len(warnings)})

    assert len(results) == 4
    assert sum(result["quality"] == "pass" for result in results) == 4
    assert sum(result["warnings"] > 0 for result in results) == 1


def test_metadata_benchmark_catches_mode_and_compliance_regressions(app_module, benchmark_manifest):
    commercial = next(item for item in benchmark_manifest["records"] if item["mode"] == "🎨 Commercial Background")
    ranked = app_module.rank_keywords_for_stock(commercial["keywords"], "📷 General Photography")
    mode_issues = app_module.validate_stock_metadata(
        commercial["title"],
        commercial["description"] + " Product display backdrop for website banners and catalog layouts.",
        ranked, "📷 General Photography"
    )
    assert any("commercial background" in issue.lower() for issue in mode_issues)

    general = next(item for item in benchmark_manifest["records"] if item["file"] == "01_felt_horse.jpg")
    ranked_general = app_module.rank_keywords_for_stock(general["keywords"], general["mode"])
    compliance_warnings = app_module.metadata_compliance_warnings(
        general["title"], general["description"], ranked_general, {"visible_logo": True}
    )
    assert any("trademark" in warning.lower() for warning in compliance_warnings)
