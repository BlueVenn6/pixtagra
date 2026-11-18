"""One-session authorized probe; outputs never include credential values."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pixtagra as app_module
from pixtagra_credential_policy import CredentialPolicyCoordinator
from pixtagra_credentials import WindowsCredentialManagerBackend
from pixtagra_windows_hello import WindowsHelloConsentBackend
from pixtagra_live_credentials import CredentialConsentBridge

def main():
    OUT = ROOT / ".build-rc" / "model-compatibility"
    OUT.mkdir(parents=True, exist_ok=True)
    state = {"stage": "authorizing", "providers": {}, "records": [], "source_hashes": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ("pixtagra.py", "pixtagra_keywords.py", "pixtagra_models.py", "pixtagra_metadata.py")}}
    def save():
        temp = OUT / "live-retest.tmp"
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(OUT / "live-retest.json")

    config = app_module._read_config_mapping(app_module.CONFIG_FILE)
    root = tk.Tk()
    root.title("PixTagra API quality check")
    tk.Label(root, text="Authorized image quality test\nUnlock saved API credentials using Windows Hello.", padx=25, pady=25).pack()
    root.update()
    root.lift()
    root.attributes("-topmost", True)
    root.focus_force()
    coordinator = CredentialPolicyCoordinator(hello_backend=WindowsHelloConsentBackend(), credential_backend=WindowsCredentialManagerBackend(), migration_engine=None)
    bridge = CredentialConsentBridge(root, coordinator)
    leases = {}
    providers = iter(("openai", "qwen", "kimi", "minimax"))

    def authorize_next():
        provider = next(providers, None)
        if provider is None:
            state["stage"] = "testing"
            save()
            threading.Thread(target=run, daemon=True).start()
            root.after(100, poll)
            return
        def complete(result):
            state["providers"][provider] = {"status": result.status, "backend_status": result.backend_status, "hello_status": result.hello_status, "error_type": result.error_type}
            if result.lease:
                leases[provider] = result.lease
            save()
            if result.status in {"hello_unavailable", "consent_not_verified"}:
                state["stage"] = "credential_unlock_unavailable"
                save()
                tk.Button(root, text="Retry authorization", command=lambda: bridge.start("batch", provider, root.winfo_id(), on_complete=complete)).pack()
            else:
                root.after(0, authorize_next)
        bridge.start("batch", provider, root.winfo_id(), on_complete=complete)

    def run():
        state["stage"] = "waiting_for_followup"
        save()


    def followup(job):
        import importlib
        import pixtagra_quality
        importlib.reload(pixtagra_quality)
        import pixtagra_keywords
        import pixtagra_models
        importlib.reload(pixtagra_keywords)
        importlib.reload(pixtagra_models)
        importlib.reload(app_module)
        app = object.__new__(app_module.App)
        app._config_data = {k:v for k,v in config.items() if k != "api_key"}
        app.key_var = SimpleNamespace(get=lambda: "")
        app.error_lock = threading.Lock()
        app.api_errors = []
        app.stop_flag = False
        app._stop_event = threading.Event()
        app.mode_var = SimpleNamespace(get=lambda: app_module.PROMPT_MODES[0])
        app._log_api_event = lambda *args, **kwargs: None
        app._log_api_debug_preview = lambda *args, **kwargs: None
        name = job["model"]
        provider = app_module.get_provider_config(name)["provider"]
        if provider not in leases:
            raise RuntimeError("No authorized credential lease for requested provider")
        key = leases[provider].secret_for_authorized_use()
        base_url = app_module.configured_base_urls(config).get(provider, "")
        record = {"endpoint_origin": __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(app._resolve_cloud_base_root(name, base_url)).hostname, "request_model": app._resolve_request_model(name), "model":name, "phase":job.get("phase"), "source_hashes":{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in ("pixtagra.py","pixtagra_keywords.py","pixtagra_models.py","pixtagra_quality.py")}}
        started = time.monotonic()
        try:
            if job.get("operation") == "catalog":
                url = app._resolve_cloud_base_root(name, base_url)+"/models"
                response = app_module.requests.get(url, headers={"Authorization":"Bearer "+key},timeout=(15,30))
                record.update(status_code=response.status_code, models=[v.get("id") for v in response.json().get("data",[])])
            else:
                path=ROOT/job["image"]
                record.update(image=path.name,image_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                b64=base64.b64encode(app_module.prepare_image(path.read_bytes())).decode()
                if job.get("keywords") or job.get("metadata"):
                    raw=job.get("metadata") or {"title":"", "description":"", "keywords": job["keywords"]}
                    result=app._review_generated_keywords(raw,b64,name,key,base_url,app_module.get_provider_config(name))
                else:
                    app._batch_visual_review_enabled=True
                    result=app._process(SimpleNamespace(path=str(path),removed=False),name,app_module.PROMPT_MODES[0],key,"",base_url)
                    raw=None
                record.update(status="returned_metadata",raw=raw,result=result)

        except Exception as exc:
            record.update(status="failed",error_type=type(exc).__name__,error=app._redact_secrets(str(exc),[key]))
        record["api_errors"] = list(app.api_errors)
        record["elapsed_seconds"]=round(time.monotonic()-started,2)
        state["records"].append(record)
        state["stage"]="waiting_for_followup"
        save()

    def poll():
        if (OUT / "finish-live").exists() and state["stage"] == "waiting_for_followup":
            for lease in leases.values():
                lease.close()
            state["stage"] = "finished"
            save()
        job_path = OUT / "followup-job.json"
        if state["stage"] == "waiting_for_followup" and job_path.exists():
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job_path.replace(OUT / ("job-"+str(time.time_ns())+".json"))
            state["stage"] = "followup"
            threading.Thread(target=lambda: followup(job), daemon=True).start()
        if state["stage"] == "finished":
            bridge.shutdown()
            root.destroy()
        else:
            root.after(100, poll)

    save()
    def start_when_active():
        state["owner_state"] = WindowsHelloConsentBackend().inspect_window(root.winfo_id()).__dict__
        save()
        if state["owner_state"].get("is_foreground"):
            root.attributes("-topmost", False)
            authorize_next()
        else:
            root.after(300, start_when_active)

    root.after(1000, start_when_active)
    root.mainloop()


if __name__ == "__main__":
    main()
