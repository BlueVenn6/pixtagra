"""Opt-in paid evaluation reusing the user's Windows-stored relay token.

Development tool only. No credential files or secrets in results. Starting this
tool authorizes the fixed evaluation; an existing Windows credential is reused
without another credential dialog. Uses the production generation/repair/review
path, with a budget gate at EVERY HTTP POST (including retries and negotiation).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://dragtokens.com"
MODEL = "OpenAI GPT-5.6 Terra"
MODEL_ID = "gpt-5.6-terra"


def write_evaluation_state(output, state):
    """Keep atomic results when a brief Windows reader denies replacement."""
    temp = output / "results.tmp"
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf8")
    for attempt in range(5):
        try:
            temp.replace(output / "results.json")
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.025 * (attempt + 1))


class BenchmarkCredentials:
    """Use the existing OS vault, never a plaintext evaluation key file."""

    def __init__(self, backend):
        self.backend = backend

    def load(self):
        from pixtagra_credential_policy import CredentialUseLease
        result = self.backend.read("openai")
        if result.status == "found" and result.secret:
            return CredentialUseLease("openai", result.secret)
        return None

    def remember(self, lease):
        result = self.backend.write("openai", lease.secret_for_authorized_use(), allow_replace=True)
        return result.verified and result.status in {"stored", "already_present"}


class BudgetStop(RuntimeError):
    pass


def fetch_billing_json(path, secret=None):
    import requests
    response = requests.get(ORIGIN + path,
        headers={"Authorization": "Bearer " + secret} if secret else {},
        timeout=(10, 20), allow_redirects=False)
    # This optional account API can reject a token that works for model APIs.
    if response.status_code == 401 and path == "/api/usage/token/":
        return {"data": {"usage_lookup_unavailable": True}}
    if response.status_code != 200:
        raise BudgetStop("计费预检查失败（HTTP " + str(response.status_code) + "），未开始付费评测")
    return response.json()


class Budget:
    # Reserve far above the published base tariff for a <=768px production image.
    # Eightfold tariff margin; failed/unknown usage keeps the full reservation.
    RESERVE = (50000 * 2.5 + 4096 * 12) / 1_000_000 * 8

    def __init__(self, limit=10.0):
        # None is only selected by the explicit --no-budget-limit CLI option.
        if limit is not None and not 0 < limit <= 10:
            raise ValueError("Authorized budget must be positive and <=10 yuan")
        self.limit = limit
        self.charged_bound = 0.0
        self.entries = []

    @classmethod
    def restore(cls, snapshot, *, unlimited=False):
        """A restart must retain all previous/unknown charges in this approval."""
        import math
        previous_limit = snapshot["limit_yuan"]
        if previous_limit is None and not unlimited:
            raise BudgetStop("An unlimited ledger requires explicit --no-budget-limit authorization")
        budget = cls(None if unlimited else min(10.0, previous_limit))
        entries = json.loads(json.dumps(snapshot["requests"]))
        total = 0.0
        for entry in entries:
            reserved, bound = entry["reserved_yuan"], entry["bound_yuan"]
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in (reserved, bound)):
                raise BudgetStop("Invalid previous budget ledger; refusing a fresh allowance")
            total += bound if entry.get("settled") is True else max(reserved, bound)
        previous = snapshot["conservative_bound_yuan"]
        if not isinstance(previous, (int, float)) or not math.isfinite(previous) or previous < 0:
            raise BudgetStop("Invalid previous budget total")
        budget.entries = entries
        budget.charged_bound = max(total, previous)
        return budget

    def reserve(self, url, payload):
        target = urlsplit(url)
        if (target.scheme, target.netloc) != ("https", "dragtokens.com"):
            raise BudgetStop("Unapproved endpoint")
        if payload.get("model") != MODEL_ID or payload.get("service_tier") not in (None, "default"):
            raise BudgetStop("Unapproved model or tariff")
        cap = payload.get("max_output_tokens", payload.get("max_tokens", payload.get("max_completion_tokens")))
        if not isinstance(cap, int) or not 1 <= cap <= 4096:
            raise BudgetStop("An explicit output bound <=4096 is required")
        if self.limit is not None and self.charged_bound + self.RESERVE > self.limit:
            raise BudgetStop("10 yuan budget: insufficient reserve for another request")
        self.charged_bound += self.RESERVE
        entry = {"reserved_yuan": self.RESERVE, "bound_yuan": self.RESERVE, "settled": False}
        self.entries.append(entry)
        return entry

    def settle(self, entry, usage):
        if not isinstance(usage, dict):
            return  # Unknown/failed requests keep their FULL reservation.
        p = usage.get("input_tokens", usage.get("prompt_tokens"))
        c = usage.get("output_tokens", usage.get("completion_tokens"))
        if any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in (p, c)):
            return
        if self.limit is not None and (p > 50000 or c > 4096):
            raise BudgetStop("Unexpected usage exceeded the conservative per-request bound")
        bound = (p * 2.5 + c * 12) / 1_000_000 * 8
        self.charged_bound += bound - entry["bound_yuan"]
        entry.update(bound_yuan=bound, settled=True, input_tokens=p, output_tokens=c)


def main():
    import argparse
    import tkinter as tk
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", help="Optional comma-separated case ids for a focused retest")
    parser.add_argument("--languages", default="en,zh")
    parser.add_argument("--no-budget-limit", action="store_true",
                        help="Explicit user-authorized evaluation without a monetary ceiling")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf8"))
    cases = manifest["cases"]
    if len(cases) != 20 or [c["mode"] for c in cases].count(0) != 10:
        raise ValueError("A fixed 10 photography / 10 background set is required")
    languages = args.languages.split(",")
    if not languages or len(languages) != len(set(languages)) or not set(languages) <= {"en", "zh"}:
        raise ValueError("Languages must be en and/or zh")
    for case in cases:
        path = (args.manifest.parent / case["file"]).resolve()
        if not path.is_relative_to(args.manifest.parent.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != case["sha256"]:
            raise ValueError("Evaluation image identity mismatch")
    if args.cases:
        selected = {int(v) for v in args.cases.split(",")}
        if not selected or not selected <= {c["id"] for c in cases}:
            raise ValueError("Unknown evaluation case ids")
        cases = [c for c in cases if c["id"] in selected]
    sys.path.insert(0, str(ROOT))
    import pixtagra as module
    from pixtagra_credential_policy import CredentialUseLease
    from pixtagra_credentials import WindowsCredentialManagerBackend

    args.output.mkdir(parents=True, exist_ok=True)
    state = {"stage": "loading_windows_credential", "model": MODEL, "endpoint": ORIGIN,
             "limit_yuan": None if args.no_budget_limit else 10, "requests": [], "records": [],
             "monetary_limit_disabled_by_explicit_option": args.no_budget_limit,
             "selected_cases": [c["id"] for c in cases], "languages": languages,
             "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
             "source_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob("pixtagra*.py")}}
    budget = Budget(None if args.no_budget_limit else 10)
    previous_path = args.output / "results.json"
    if previous_path.exists():
        previous = json.loads(previous_path.read_text(encoding="utf8"))
        if previous.get("requests"):
            budget = Budget.restore(previous, unlimited=args.no_budget_limit)
            import time
            archive = args.output / f"previous-{time.time_ns()}.json"
            archive.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf8")
            state["previous_evaluation"] = {"file": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                                           "bound_yuan": budget.charged_bound}
    messages = queue.Queue()
    stop = threading.Event()
    active = [False]

    def save():
        state["requests"] = budget.entries
        state["conservative_bound_yuan"] = round(budget.charged_bound, 6)
        state["actual_charge_yuan"] = None
        state["bound_is_not_actual_charge"] = True
        write_evaluation_state(args.output, state)

    root = tk.Tk()
    from pixtagra_branding import apply_app_icon
    apply_app_icon(root)
    root.title("PixTagra · 实图质量评测（不限金额·源码）" if args.no_budget_limit
               else "PixTagra · 10元上限真实质量评测（源码）")
    root.geometry("760x420")
    root.option_add("*Font", ("Microsoft YaHei UI", 12))
    tk.Label(root, justify="left", wraplength=710, text=(
        f"{len(cases)}张固定图片 × {'中英文' if len(languages) == 2 else languages[0]}；GPT-5.6 Terra / 当前中转。\n"
        + ("本轮已授权取消金额上限，按固定样本完成后停止；记录请求和 token 用量。\n"
           if args.no_budget_limit else "每次请求预留保守费用上界，累计预算最多10元。\n") +
        "自动复用 Windows 凭据管理器中的密钥，不再重复要求输入；不读取密钥文件。\n"
        + ("不依赖计费接口，不将预留估价当成实际扣款。" if args.no_budget_limit
           else "公开价格或币种无法核实时不调用；额度查询不可用会单独记录。")), padx=20, pady=18).pack()
    key_var = tk.StringVar()
    credential_input = tk.Frame(root)
    tk.Entry(credential_input, textvariable=key_var, show="*", width=55).pack(side="left", padx=8)
    status = tk.StringVar(value="正在复用 Windows 已存密钥，无需再次输入。")
    tk.Label(root, textvariable=status, wraplength=710, justify="left", padx=18, pady=15).pack()
    credentials = BenchmarkCredentials(WindowsCredentialManagerBackend())

    def run(lease):
        secret = lease.secret_for_authorized_use()
        try:
            state.pop("reason", None)
            state.pop("dispatch_stop_reason", None)
            state["stage"] = "preflight"
            save()
            if args.no_budget_limit:
                state["billing_preflight"] = {"skipped": True, "reason": "user_authorized_no_monetary_limit"}
            else:
                status_data = fetch_billing_json("/api/status")["data"]
                if status_data.get("quota_display_type") != "CNY" or status_data.get("quota_per_unit") != 500000 or status_data.get("usd_exchange_rate") != 1:
                    raise BudgetStop("The relay billing currency/unit changed; no paid request sent")
                pricing = fetch_billing_json("/api/pricing")
                rows = [r for r in pricing["data"] if r.get("model_name") == MODEL_ID]
                expression = rows[0].get("billing_expr", "") if len(rows) == 1 else ""
                if "p * 2 + c * 12" not in expression or "cr * 0.2 + cc * 2.5" not in expression:
                    raise BudgetStop("The published Terra tariff changed; no paid request sent")
                ratios = pricing.get("group_ratio", {})
                if not ratios or any(not isinstance(r, (int, float)) or not 0 < r <= 8 for r in ratios.values()):
                    raise BudgetStop("The published group tariff exceeds the conservative budget margin")
                token = fetch_billing_json("/api/usage/token/", secret)["data"]
                available = token.get("total_available")
                state["billing_preflight"] = {"quota_per_unit": 500000, "currency": "CNY", "limited_token": True,
                    "usage_lookup_available": not token.get("usage_lookup_unavailable", False),
                    "pricing_version": pricing.get("pricing_version"), "group_ratios": ratios,
                    "tariff_expression": expression, "budget_method": "eightfold_tariff_upper_bound"}
                state["billing_preflight"]["limited_token"] = token.get("unlimited_quota") is False
                if token.get("unlimited_quota") is False and isinstance(available, (int, float)):
                    budget.limit = min(10.0, max(0, available / 500000))
            messages.put(None)  # Erase the input only after successful preflight.
            app = object.__new__(module.App)
            app._config_data = {}
            app.key_var = SimpleNamespace(get=lambda: "")
            app.error_lock = threading.Lock()
            app.api_errors = []
            app._stop_event = stop
            app.stop_flag = False
            app.mode_var = SimpleNamespace(get=lambda: module.PROMPT_MODES[0])
            app._log_api_event = lambda *a, **kw: None
            app._log_api_debug_preview = lambda *a, **kw: None
            app._checkpoint_result = lambda *a: None
            app._batch_visual_review_enabled = True
            original_post = app._post_if_active
            def post(session, url, **kwargs):
                if stop.is_set():
                    raise BudgetStop("Stopped")
                try:
                    entry = budget.reserve(url, kwargs.get("json", {}))
                    entry.update(case=app._evaluation_case, language=app._batch_language)
                except BudgetStop as exc:
                    state["dispatch_stop_reason"] = str(exc)
                    stop.set()
                    app.stop_flag = True
                    raise
                save()  # Reserve before dispatch, including if the process crashes.
                try:
                    response = original_post(session, url, **kwargs)
                    entry["http_status"] = response.status_code
                    try:
                        budget.settle(entry, response.json().get("usage"))
                    except (ValueError, AttributeError):
                        pass
                    return response
                except BudgetStop as exc:
                    state["dispatch_stop_reason"] = str(exc)
                    stop.set()
                    app.stop_flag = True
                    raise
                finally:
                    save()
            app._post_if_active = post
            state["stage"] = "running"
            for position, case in enumerate(cases, 1):
                for language in languages:
                    if any(r["case"] == case["id"] and r["language"] == language and r["result"] is not None
                           for r in state["records"]):
                        continue
                    if stop.is_set():
                        raise BudgetStop("Stopped; previous results retained")
                    app._batch_language = language
                    app._evaluation_case = case["id"]
                    app.mode_var = SimpleNamespace(get=lambda c=case: module.PROMPT_MODES[c["mode"]])
                    progress = f"正在评测第{position}/{len(cases)}张（样本{case['id']}）· {language}"
                    messages.put(progress + (f"；已发送{len(budget.entries)}次请求" if args.no_budget_limit
                                             else f"；累计保守费用上界{budget.charged_bound:.2f}元"))
                    path = args.manifest.parent / case["file"]
                    app.api_errors.clear()
                    result = app._process(SimpleNamespace(path=str(path), removed=False), MODEL,
                        module.PROMPT_MODES[case["mode"]], secret, "", ORIGIN + "/v1")
                    state["records"].append({"case": case["id"], "language": language, "result": result,
                        "errors": [app._redact_secrets(e, [secret]) for e in app.api_errors]})
                    save()
                    if result is None:
                        raise BudgetStop(state.get("dispatch_stop_reason", "A real generation failed; stopped for investigation"))
            state["stage"] = "generated_pending_human_quality_review"
        except Exception as exc:
            state["stage"] = "stopped"
            state["reason"] = str(exc).replace(secret, "[redacted]") if secret else type(exc).__name__
        finally:
            lease.close()
            secret = None
            save()
            messages.put(state.get("reason", f"{len(state['records'])}份结果已记录，等待逐词质量验收。"))
            active[0] = False

    def begin(lease):
        if active[0]:
            lease.close()
            return
        active[0] = True
        stop.clear()
        status.set("正在开始固定样本实图评测（本轮不设金额上限）。" if args.no_budget_limit
                   else "正在核对公开价格和预算；通过后开始评测。此步骤不会调用付费模型。")
        threading.Thread(target=run, args=(lease,), daemon=True).start()

    def pasted():
        if active[0]:
            return
        if key_var.get().strip():
            lease = CredentialUseLease("openai", key_var.get().strip())
            if not credentials.remember(lease):
                lease.close()
                status.set("Windows 未能保存密钥，尚未开始；可保留当前输入后重试。")
                return
            key_var.set("")
            credential_input.pack_forget()
            begin(lease)
        else:
            status.set("请先输入模型令牌，再点击开始。")

    def saved():
        if active[0]:
            return
        lease = credentials.load()
        if lease:
            begin(lease)
        else:
            credential_input.pack(pady=8)
            state["stage"] = "windows_credential_unavailable"
            save()
            status.set("Windows 中没有可用密钥；首次输入后会安全保存，后续自动复用。")

    def change_key():
        if not active[0]:
            credential_input.pack(pady=8)
            status.set("只有主动更换密钥才需要输入；保存后将用于后续评测和程序。")

    buttons = tk.Frame(root)
    buttons.pack()
    tk.Button(credential_input, text="安全保存并开始", command=pasted).pack(side="left", padx=8)
    tk.Button(buttons, text="使用已存密钥继续评测", command=saved).pack(side="left", padx=8)
    tk.Button(buttons, text="更换评测密钥", command=change_key).pack(side="left", padx=8)
    tk.Button(root, text="停止后续请求并保留结果", command=stop.set).pack(pady=14)
    def poll():
        while not messages.empty():
            message = messages.get_nowait()
            if message is None:
                key_var.set("")
            else:
                status.set(message)
        root.after(150, poll)
    def close():
        stop.set()
        if active[0]:
            status.set("等待当前请求返回后再关闭；后续请求已停止。")
        else:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    save()
    root.after(150, poll)
    root.after(200, saved)
    root.mainloop()


if __name__ == "__main__":
    main()
