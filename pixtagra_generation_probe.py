"""Explicit isolated GUI -> loopback HTTP -> metadata/export acceptance probe.

This does not qualify any real model or provider account. No user credentials,
user images or external endpoints are used. Production generation code runs.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import ssl
import ipaddress
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def run_probe(module, output_dir):
    if not module._packaged_test_isolation_enabled():
        raise RuntimeError("Generation probe requires explicit test isolation")
    from PIL import Image, ImageGrab
    from pixtagra_session import SessionStore
    from pixtagra_models import OPENAI_GPT_56_CANDIDATES
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    case = os.environ.get("PIXTAGRA_GENERATION_PROBE_CASE", "gpt-5.6-sol")
    if case not in {"locked", *(model_id for _, model_id in OPENAI_GPT_56_CANDIDATES)}:
        raise ValueError("Unknown generation probe case")
    metadata = {
        "title": "Mountain and Lake Landscape beneath Clouds in Evening Light",
        "description": "Mountains rise above a lake with trees along the water and clouds overhead. Evening light falls across the landscape, showing the rocky slopes and the still water in a wide view.",
        "keywords": ["mountain", "lake", "forest", "water", "clouds", "sky", "grass", "trees",
                     "rocks", "shoreline", "reflection", "valley", "peak", "meadow", "wilderness",
                     "nature", "outdoors", "scenery", "panorama", "horizon", "daylight", "sunlight",
                     "summer", "spring", "green", "blue", "white", "terrain", "slope", "ridge",
                     "foliage", "evergreen", "rural", "vista", "landscape"],
    }
    calls, dialogs, errors = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            content = [part for item in request["input"] for part in item["content"]]
            prompt = next(part["text"] for part in content if part["type"] == "input_text")
            has_image = any(part["type"] == "input_image" and part["image_url"].startswith("data:image/") for part in content)
            response = metadata
            phase = "smoke" if "Analyze this simple test image" in prompt else "generation"
            if "CANDIDATES:\n" in prompt:
                candidates = json.JSONDecoder().raw_decode(prompt.split("CANDIDATES:\n", 1)[1])[0]
                phase = "review"
                response = {
                    "visible_facts": ["Synthetic loopback landscape fixture", "Synthetic mountain and lake"],
                    "duplicate_groups": [],
                    "checks": [{"id": row["id"], "verdict": "supported", "reason": "Synthetic loopback fixture"} for row in candidates],
                    "caption_checks": [{"field": field, "verdict": "supported", "reason": "Synthetic loopback fixture"} for field in ("title", "description")],
                }
            if prompt.startswith("Your ONLY task is semantic deduplication"):
                phase = "deduplication"
                response = {"duplicate_groups": []}
            calls.append({"model": request["model"], "path": self.path, "phase": phase, "image": has_image})
            body = json.dumps({"output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": json.dumps(response)}]}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    tls = tempfile.TemporaryDirectory(prefix="pixtagra-loopback-tls-")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "PixTagra isolated loopback")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = Path(tls.name) / "cert.pem", Path(tls.name) / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    previous_ca = os.environ.get("REQUESTS_CA_BUNDLE")
    os.environ["REQUESTS_CA_BUNDLE"] = str(cert_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original_dialogs = {name: getattr(module.messagebox, name) for name in ("showinfo", "showwarning", "showerror")}
    original_open = module.filedialog.askopenfilenames
    original_save = module.filedialog.asksaveasfilename
    for name in original_dialogs:
        setattr(module.messagebox, name, lambda *a, _name=name, **k: dialogs.append((_name, a)))

    def pump(app, until, seconds=40):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.update()
            if until():
                return
            time.sleep(.005)
        raise TimeoutError("Generation GUI condition did not complete")

    def new_app(folder):
        module.APP_CONFIG_DIR = folder
        module.CONFIG_FILE = folder / "config.json"
        module.VAULT_DIR = folder / "unused-vault"
        module.VAULT_FILE = module.VAULT_DIR / "keys.vault"
        module.SALT_FILE = module.VAULT_DIR / "keys.salt"
        app = module.App()
        app.title("PixTagra — isolated generation / loopback HTTP / no real API")
        app.report_callback_exception = lambda *exc: errors.append(type(exc[1]).__name__ + ": " + str(exc[1]))
        return app

    outcomes = []
    blocked_visible = None
    try:
        with tempfile.TemporaryDirectory(prefix="pixtagra-generation-") as scratch:
            root = Path(scratch)
            image = root / "synthetic.png"
            Image.new("RGB", (640, 480), "blue").save(image)
            module.filedialog.askopenfilenames = lambda **kw: [str(image)]
            if case == "locked":
                blocked = root / "blocked"
                owner = SessionStore(blocked / "workspace.sqlite3")
                app = new_app(blocked)
                try:
                    app.load_images()
                    pump(app, lambda: not app.is_importing)
                    before = len(dialogs)
                    app.btns["⚡ Analyze"].invoke()
                    app.update()
                    blocked_visible = len(dialogs) == before + 1 and "Generation not started" in app.prog_lbl.cget("text")
                    assert blocked_visible and not app.is_processing and not calls
                    assert app.__dict__.get("_session_store") is None
                    assert owner.load()[0]["records"] == []
                finally:
                    app.destroy()
                    owner.close()
            for name, model_id in OPENAI_GPT_56_CANDIDATES:
                if case != model_id:
                    continue
                folder = root / model_id
                app = new_app(folder)
                try:
                    app.model_var.set(name)
                    app._on_model_change()
                    app.base_url_var.set(f"https://127.0.0.1:{server.server_port}/v1")
                    app.key_var.set("synthetic-local-probe-key")
                    app.visual_review_var.set(True)
                    app.load_images()
                    pump(app, lambda: not app.is_importing)
                    start = len(calls)
                    # Invoke the production connection action and wait for its actual HTTP response.
                    app._test_connection()
                    pump(app, lambda: len(calls) > start and not app._network_check_busy)
                    assert "Connection OK" in dialogs[-1][1][1]
                    app.btns["⚡ Analyze"].invoke()
                    pump(app, lambda: not app.is_processing and app.stats["processed"] == 1)
                    card = app.cards[str(image)]
                    assert card._cached_data["title"] == metadata["title"]
                    assert card._cached_data["description"] == metadata["description"]
                    assert card._cached_data["keywords"]
                    assert card._cached_data["keyword_review"]["status"] == "reviewed"
                    app._focus_card(card)
                    app.update()
                    assert card.view.title_e.get() == metadata["title"]
                    assert metadata["description"] in card.view.desc_e.get("1.0", "end")
                    exported = folder / "export.json"
                    module.filedialog.asksaveasfilename = lambda **kw: str(exported)
                    app._export()
                    assert json.loads(exported.read_text())[0]["title"] == metadata["title"]
                    snapshot, pending = app._session_store.load()
                    assert snapshot["records"][0]["data"]["title"] == metadata["title"] and not pending
                    model_calls = calls[start:]
                    assert all(call["model"] == model_id and call["image"] for call in model_calls)
                    assert {call["phase"] for call in model_calls} == {"smoke", "generation", "review", "deduplication"}
                    assert not errors and not app.api_errors
                    box = (app.winfo_rootx(), app.winfo_rooty(), app.winfo_rootx() + app.winfo_width(), app.winfo_rooty() + app.winfo_height())
                    ImageGrab.grab(bbox=box).save(output / (model_id + ".png"))
                    outcomes.append({"model": model_id, "calls": model_calls, "processed": app.stats["processed"],
                                     "visible_title": True, "visible_description": True, "keywords": len(card._cached_data["keywords"]),
                                     "review": "reviewed", "export": True, "durable_result": True})
                finally:
                    app.destroy()
        return {"passed": True, "blocked_generation_visible": blocked_visible, "models": outcomes,
                "callback_errors": errors, "external_api_requests": 0, "real_credentials_access": 0,
                "transport": "real loopback HTTPS; synthetic Responses payloads", "live_provider_verified": False}
    finally:
        server.shutdown()
        server.server_close()
        if previous_ca is None:
            os.environ.pop("REQUESTS_CA_BUNDLE", None)
        else:
            os.environ["REQUESTS_CA_BUNDLE"] = previous_ca
        tls.cleanup()
        for name, fn in original_dialogs.items():
            setattr(module.messagebox, name, fn)
        module.filedialog.askopenfilenames = original_open
        module.filedialog.asksaveasfilename = original_save


if __name__ == "__main__":
    import sys
    os.environ["PIXTAGRA_PACKAGED_TEST_ISOLATION"] = "1"
    os.environ["PSS_NO_STARTUP_DIALOG"] = "1"
    import pixtagra
    result = run_probe(pixtagra, sys.argv[1])
    print(json.dumps(result))
    (Path(sys.argv[1]) / "generation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
