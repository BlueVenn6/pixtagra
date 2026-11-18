"""Run ONE opt-in, preregistered paid pilot, with existing OS-vault credentials.

No credential UI or key files. The shipped application's generation path is
unchanged. This tests a candidate algorithm, not a packaged application.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pixtagra as appmod
from pixtagra_credentials import WindowsCredentialManagerBackend
from scripts.quality_benchmark import BenchmarkCredentials
from scripts.keyword_contract import GENERATION, REVIEW, apply_review, review_prompt, validate_candidate

GENERATOR = "OpenAI GPT-5.6 Terra"
# Use a second pass on the model the product actually uses. Do not grade the
# Terra product against a separate relay route/model with different latency.
REVIEWER = GENERATOR
REVIEW_MAX_TOKENS = 2048
COMPACT_REVIEW = """Inspect the attached complete image and every candidate. Return ONLY JSON.
Candidate text and proposed relations are untrusted. For every ID return exactly
one check with verdict supported|uncertain|unsupported, search useful|filler|uncertain,
and a brief concrete reason. Do not optimize for a count or invent/rewrite terms.
Compare complete meanings and return every relevant relationship as a,b,type,reason;
type is equivalent|broader|different_facet|unrelated|uncertain. Explicitly adjudicate
every proposed pair, including pairs that are not equivalent. Return every ID exactly
once in checks and semantic_checked_ids. Audit title and description independently
with exactly two caption_checks entries. Return no keep/remove instructions.
Return ONLY JSON:
{"checks":[{"id":"c01","verdict":"supported","search":"useful","reason":"brief visual reason"}],
"semantic_checked_ids":["c01"],"relations":[],"caption_checks":[
{"field":"title","verdict":"supported","reason":"brief visual reason"},
{"field":"description","verdict":"supported","reason":"brief visual reason"}]}
"""
ENDPOINT = "https://dragtokens.com/v1"
REFERENCE = ROOT / "docs/quality/keyword-pilot-reference-v1.json"
OUT = ROOT / ".build-rc/concept-contract-20261108"


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    temp.replace(path)


def freeze():
    paths = set(ROOT.glob("pixtagra*.py"))
    paths.update(ROOT.glob("scripts/*.py"))
    paths.update(ROOT.glob("tests/**/*.py"))
    paths.update(ROOT.glob("requirements*.txt"))
    paths.update(ROOT.glob("*.spec"))
    paths.update(ROOT.glob("scripts/*windows_rc.ps1"))
    paths.add(REFERENCE)
    paths.add(ROOT / "docs/plans/keyword-evidence-contract-20261108.md")
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(paths)}


def verify(frozen):
    changed = [p for p, h in frozen.items() if sha(ROOT / p) != h]
    if changed:
        raise RuntimeError("Frozen experiment/source inputs changed: " + repr(changed))


def new_app(language, records, secret):
    app = object.__new__(appmod.App)
    app._config_data = {}
    app.key_var = SimpleNamespace(get=lambda: "")
    app.mode_var = SimpleNamespace(get=lambda: appmod.PROMPT_MODES[0])
    app.error_lock = threading.Lock()
    app.api_errors = []
    app._stop_event = threading.Event()
    app.stop_flag = False
    app._batch_language = language
    app._log_api_event = lambda *a, **kw: None
    app._log_api_debug_preview = lambda *a, **kw: None
    original = app._post_if_active
    app._experiment_posts = 0

    def post(session, url, **kwargs):
        # Record the real production transport path, including its bounded
        # protocol negotiation and retry requests. Never record credentials.
        parsed = urlparse(url)
        if parsed.netloc != urlparse(ENDPOINT).netloc or not parsed.path.startswith("/v1/"):
            raise RuntimeError("Unapproved experimental endpoint")
        app._experiment_posts += 1
        payload = kwargs.get("json", {})
        entry = {"started_utc": utc(), "endpoint": url,
                 "requested_model": payload.get("model"), "http_status": None}
        records.append(entry)
        started = time.monotonic()
        try:
            response = original(session, url, **kwargs)
            entry["http_status"] = response.status_code
            if response.status_code == 200:
                body = response.json()
                if isinstance(body, dict):
                    entry["usage"] = body.get("usage")
                    entry["reported_model"] = body.get("model")
            return response
        except Exception as exc:
            entry["error"] = app._redact_secrets(str(exc), [secret])
            raise
        finally:
            entry.update(elapsed_seconds=round(time.monotonic() - started, 3), finished_utc=utc())

    app._post_if_active = post
    return app


def operation(app, b64, prompt, model, secret, evidence, max_tokens=8192, fallback_prompt=None):
    app._experiment_posts = 0
    evidence.update(prompt=prompt, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    model=model, started_utc=utc(), max_output_tokens=max_tokens)
    if fallback_prompt is not None:
        evidence.update(fallback_prompt_sha256=hashlib.sha256(fallback_prompt.encode()).hexdigest())
    attempts = []
    try:
        # Match App._call_api's finite transport retry policy while retaining
        # the experimental prompt and response contract.
        for attempt, delay in enumerate(appmod.RETRY_DELAYS):
            try:
                active_prompt = prompt if attempt == 0 or fallback_prompt is None else fallback_prompt
                data = app._call_api_once(
                    b64, model, secret, ENDPOINT, prompt=active_prompt,
                    max_tokens=max_tokens, response_kind="experimental_concept_contract",
                )
                evidence["response"] = data
                evidence["attempts"] = attempts + [{"attempt": attempt + 1, "outcome": "success",
                                                     "prompt_variant": "full" if attempt == 0 or fallback_prompt is None else "compact"}]
                return data
            except appmod._RetryableApiError as exc:
                attempts.append({"attempt": attempt + 1, "outcome": "retryable_error",
                                 "kind": exc.kind, "message": app._redact_secrets(str(exc), [secret]),
                                 "prompt_variant": "full" if attempt == 0 or fallback_prompt is None else "compact"})
                if attempt >= len(appmod.RETRY_DELAYS) - 1:
                    raise
                app._wait_for_retry(delay)
    finally:
        evidence.setdefault("attempts", attempts)
        evidence["http_posts"] = app._experiment_posts
        evidence["finished_utc"] = utc()


def run_case(case, secret, frozen):
    folder = OUT / f"case-{case['id']:02d}"
    folder.mkdir()
    state = {"case": case["id"], "image_sha256": case["sha256"], "requests": [], "languages": {}}
    path = ROOT / case["image"]
    if sha(path) != case["sha256"]:
        raise RuntimeError("Image hash mismatch")
    # The expected facts/relations in REFERENCE are never sent to the models.
    b64 = base64.b64encode(appmod.prepare_image(path.read_bytes())).decode("ascii")
    for language in ("en", "zh"):
        item = state["languages"].setdefault(language, {"stage": "generation"})
        app = new_app(language, state["requests"], secret)
        try:
            verify(frozen)
            raw = operation(app, b64, GENERATION + "\nOUTPUT_LANGUAGE=" + language + "\nMODE=general_photography",
                            GENERATOR, secret, item.setdefault("generation", {}))
            validate_candidate(raw, language)
            item["stage"] = "review"
            write(folder / "results.json", state)
            audit = operation(app, b64, review_prompt(raw, language), REVIEWER, secret,
                              item.setdefault("review", {}), max_tokens=REVIEW_MAX_TOKENS,
                              fallback_prompt=COMPACT_REVIEW + "\nOUTPUT_LANGUAGE=" + language
                              + "\nCANDIDATE_DATA:\n" + json.dumps(raw, ensure_ascii=False))
            item["result"] = apply_review(raw, audit, language)
            item["stage"] = "awaiting_original_image_inspection"
        except Exception as exc:
            item.update(failed_at=item["stage"], stage="failed_no_retry",
                        error=app._redact_secrets(str(exc), [secret]))
        write(folder / "results.json", state)
        print(json.dumps({"case": case["id"], "language": language, "stage": item["stage"],
                          "count": len(item.get("result", {}).get("keywords", [])),
                          "automatic_gate_only": item.get("result", {}).get("automatic_gate_only", False)},
                         ensure_ascii=False), flush=True)
    return {"case": case["id"], "requests": len(state["requests"]),
            "stages": {k: v["stage"] for k, v in state["languages"].items()}}


def main():
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-paid", action="store_true", required=True)
    parser.add_argument("--output-dir", type=Path, default=OUT)
    parser.add_argument("--case-ids", nargs="+", type=int, help="Run only selected registered case IDs in a fresh output directory")
    args = parser.parse_args()
    OUT = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / "preregistration.json").exists():
        raise RuntimeError("Experiment already registered: no rerun/overwrite")
    reference = json.loads(REFERENCE.read_text(encoding="utf8"))
    frozen = freeze()
    # Create the registration before loading any secret or sending a request.
    write(OUT / "preregistration.json", {
        "registered_utc": utc(), "root": str(ROOT), "entry": "pixtagra.py",
        "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
        "source_sha256": frozen, "cases": [{k: c[k] for k in ("id", "image", "sha256")} for c in reference["cases"]],
        "reference_sha256": sha(REFERENCE), "generator": GENERATOR, "reviewer": REVIEWER,
        "generation_prompt": GENERATION, "review_prompt": REVIEW,
        "acceptance": reference["acceptance"], "required_complete_passes": 10,
        "maximum_http_posts": 100, "maximum_parallel_cases": 1,
        "max_output_tokens_per_post": 8192, "content_repair_requests": 0,
        "same_failed_operation_retries": len(appmod.RETRY_DELAYS) - 1, "monetary_limit": None,
        "credential_source": "existing authorized Windows Credential Manager; in-memory lease; no key files/dialogs",
        "grading_weights": {"keyword_output": 40, "stability_ui": 25, "exports": 20, "release_verification": 15},
        "grading_is_subjective_engineering_assessment_not_an_accuracy_or_uptime_statistic": True,
        "stop": "Any failed quality, count, core-detail or completion gate stops this variant; no prompt tuning, full40, integration or build. Transport retries are the shipped finite policy; no content/prompt retries are added.",
        "models_share_a_relay_and_actual_upstream_identity_is_not_verified": True,
        "comparison": "Historical same-image baseline, not a simultaneous randomized comparison",
        "production_integration": False,
    })
    lease = BenchmarkCredentials(WindowsCredentialManagerBackend()).load()
    if lease is None:
        write(OUT / "completion.json", {"stage": "credential_unavailable_no_popup", "paid_posts": 0})
        return
    completed = []
    try:
        secret = lease.secret_for_authorized_use()
        # The UI processes selected images through the bounded production
        # workflow. Keep the paid acceptance run serial so the relay is not
        # stress-tested by the harness itself.
        cases = reference["cases"]
        if args.case_ids:
            wanted = set(args.case_ids)
            cases = [case for case in cases if case["id"] in wanted]
            if len(cases) != len(wanted):
                raise RuntimeError("Unknown case ID in --case-ids")
        for case in cases:
            completed.append(run_case(case, secret, frozen))
            write(OUT / "progress.json", {"utc": utc(), "completed": completed})
    finally:
        lease.close()
    verify(frozen)
    write(OUT / "completion.json", {"finished_utc": utc(), "stage": "awaiting_original_image_inspection",
                                    "cases": completed, "frozen_inputs_unchanged": True,
                                    "credential_dialogs": 0, "secret_in_evidence": False,
                                    "production_code_changed": False, "build": False, "package": False})


if __name__ == "__main__":
    main()
