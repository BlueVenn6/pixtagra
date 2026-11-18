"""Score recorded image-review responses against a fixed, human-readable manifest.

No credentials or network access. Example:
python scripts/evaluate_visual_reviews.py --results saved-live-reviews.json --output scores.json
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pixtagra_evaluation import score_review


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "tests/fixtures/metadata_benchmark/visual_review_cases.json")
    parser.add_argument("--results", type=Path, required=True,
                        help="JSON object with records: [{model, image, result}]")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.manifest.read_text(encoding="utf-8"))["cases"]
    records = json.loads(args.results.read_text(encoding="utf-8"))["records"]
    scores = []
    for record in records:
        case = next((item for item in cases if Path(item["file"]).name == Path(record.get("image", "")).name), None)
        if case is not None:
            scores.append({"model": record["model"], **score_review(case, record.get("result"))})
    report = {"manifest": str(args.manifest), "results": str(args.results),
              "scores": scores, "passed": bool(scores) and all(score["passed"] for score in scores)}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Scored {len(scores)} records; {sum(score['passed'] for score in scores)} passed")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
