"""Write separate generation and review evidence; never confuse their counts."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from pixtagra_models import API_CONFIG, SELECTABLE_MODELS
from pixtagra_evaluation import score_review


def build_matrix(probe, cases):
    rows = []
    # Removing a model from new choices must not erase historical evidence.
    legacy_evidence = {record.get("model") for record in probe.get("records", [])
                       if isinstance(record, dict)}
    names = list(SELECTABLE_MODELS) + [name for name, config in API_CONFIG.items()
                                     if config.get("legacy_saved_only") and name in legacy_evidence]
    for name in names:
        config = API_CONFIG[name]
        generation, reviews = [], []
        for record in probe.get("records", []):
            if not isinstance(record, dict) or record.get("model") != name:
                continue
            phase = str(record.get("phase", ""))
            if phase != "review" and not phase.startswith("metadata"):
                continue
            result = record.get("result") or {}
            api_errors = record.get("api_errors") or []
            last_error = api_errors[-1] if api_errors and isinstance(api_errors[-1], dict) else {}
            item = {
                "phase": phase, "image": Path(record.get("image", "")).name,
                "image_sha256": record.get("image_sha256"),
                "status": record.get("status", "not_tested"),
                "elapsed_seconds": record.get("elapsed_seconds"),
                "error_code": record.get("error_code") or last_error.get("error_code"),
                "api_error_count": len(api_errors),
                "endpoint_origin": record.get("endpoint_origin"),
                "keyword_count": len(result.get("keywords", [])),
                "review_status": (result.get("keyword_review") or {}).get("status"),
                "source_hashes": record.get("source_hashes"),
            }
            if phase == "review":
                case = next((c for c in cases if Path(c["file"]).name == item["image"]), None)
                item["benchmark_case"] = case["id"] if case else None
                item["benchmark"] = score_review(case, result) if case else None
                reviews.append(item)
            else:
                generation.append(item)
        rows.append({"model": name, "provider": config["provider"],
                     "request_model": config["model"], "transport": config.get("protocol", "openai_compat"),
                     "release_status": config["release_status"], "review_required": config["review_required"],
                     "generation_runs": generation, "review_runs": reviews})
    return {"matrix_version": 2, "credentials_included": False, "models": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    probe = json.loads(args.probe.read_text(encoding="utf-8"))
    cases = json.loads((ROOT / "tests/fixtures/metadata_benchmark/visual_review_cases.json").read_text(encoding="utf-8"))["cases"]
    result = build_matrix(probe, cases)
    result["generated_from_probe"] = args.probe.name
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = ["# Model compatibility matrix", "", f"Probe: `{args.probe.name}`", "",
          "Generation counts and review-fixture counts are separate. Review inputs deliberately contain incorrect tags; seven retained tags in that test do not describe the generation target.", "",
          "These are a small number of authorized live calls, not a long-term availability or commercial accuracy estimate. Requests through a relay do not establish its upstream model identity.", "",
          "| Model | Generation result / keyword counts | Visual-review benchmark | Manual review policy |",
          "|---|---|---|---|"]
    for row in result["models"]:
        generated = "; ".join(f"{r['phase']}: {r['status']} / {r['keyword_count']}" for r in row["generation_runs"]) or "not tested"
        reviewed = "; ".join("passed" if (r["benchmark"] or {}).get("passed") else "failed / not comparable" for r in row["review_runs"]) or "not tested"
        md.append(f"| {row['model']} | {generated} | {reviewed} | {'required' if row['review_required'] else 'normal quality checks'} |")
    md.extend(["", "Generation aims for 35 image-supported tags. It does not pad unsupported terms to reach that number. See the JSON for endpoint, input hashes, individual failures and tested source hashes."])
    args.output.with_suffix(".md").write_text("\n".join(md) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
