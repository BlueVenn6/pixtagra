"""Score a fixed visual-review benchmark independently of the model's self-rating."""


def score_review(case, result):
    report = (result or {}).get("keyword_review", {})
    if report.get("status") != "reviewed":
        return {"case": case["id"], "complete": False, "passed": False}
    decisions = {item["keyword"]: item["verdict"] for item in report.get("checks", [])}
    positives = case.get("supported", [])
    negatives = case.get("unsupported", [])
    uncertain = case.get("uncertain", [])
    false_removals = [word for word in positives if decisions.get(word) != "supported"]
    missed_errors = [word for word in negatives if decisions.get(word) != "unsupported"]
    overconfident = [word for word in uncertain if decisions.get(word) == "supported"]
    missing = [word for word in positives + negatives + uncertain if word not in decisions]
    captions = {item["field"]: item["verdict"] for item in report.get("caption_checks", [])}
    caption_errors = [field for field, verdict in case.get("caption_verdicts", {}).items() if captions.get(field) != verdict]
    return {"case": case["id"], "complete": not missing,
            "supported_count": len(positives), "unsupported_count": len(negatives),
            "supported_recall": (len(positives) - len(false_removals)) / max(1, len(positives)),
            "error_detection_rate": (len(negatives) - len(missed_errors)) / max(1, len(negatives)),
            "false_removals": false_removals, "missed_errors": missed_errors,
            "overconfident": overconfident, "missing": missing, "caption_errors": caption_errors,
            "passed": not (false_removals or missed_errors or overconfident or missing or caption_errors)}
