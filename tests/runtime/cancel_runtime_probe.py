"""Actual Tk Cancel -> actual loopback HTTP replies -> durable workspace results."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def run(folder):
    os.environ.update(PIXTAGRA_PACKAGED_TEST_ISOLATION="1", PSS_NO_STARTUP_DIALOG="1",
                      PSS_CONFIG_DIR=str(folder), PSS_CONFIG_FILE=str(folder / "config.json"))
    import pixtagra as m
    from PIL import Image
    accepted = []
    release = threading.Event()
    data = {"title": "Mountain and Lake Landscape beneath Clouds in Evening Light",
            "description": "Mountains rise above a lake with trees along the water and clouds overhead. Evening light falls across the landscape, showing the rocky slopes and the still water in a wide view.",
            "keywords": ["mountain", "lake", "forest", "water", "clouds", "sky"]}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            accepted.append(time.monotonic())
            assert release.wait(10)
            body = json.dumps({"message": {"content": json.dumps(data)}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    m.messagebox.showwarning = lambda *args: None
    app = m.App()
    app.title("PixTagra — isolated HTTP cancellation check")
    errors = []
    app.report_callback_exception = lambda *exc: errors.append(str(exc[1]))
    for index in range(6):
        path = str(folder / f"image-{index}.png")
        Image.new("RGB", (120, 80)).save(path)
        app.cards[path] = m.CardRecord(path, app)
        app.card_order.append(path)
    app._resume_session()
    app._persist_session()
    app.is_processing = True
    app.btns["⏹ Cancel"].configure(state="normal")
    app._batch_visual_review_enabled = False
    app._analysis_result_job = app.after(25, app._drain_analysis_results)
    worker = threading.Thread(target=app._analyze_thread, args=(list(app.cards.values()), "Local Ollama",
        m.PROMPT_MODES[0], None, f"http://127.0.0.1:{server.server_port}/api/chat", ""), daemon=True)
    worker.start()
    deadline = time.monotonic() + 15
    cancelled = False
    ticks = 0
    try:
        while app.is_processing and time.monotonic() < deadline:
            app.update()
            ticks += 1
            if len(accepted) == 2 and not cancelled:
                app.btns["⏹ Cancel"].invoke()
                assert app.stop_flag
                cancelled = True
                release.set()
            time.sleep(.005)
        assert not app.is_processing and cancelled
        assert len(accepted) == 2
        assert len(app.data_cache) == 2
        assert app.stats["processed"] == 2
        snapshot, checkpoints = app._session_store.load()
        assert sum(bool(r["data"].get("title")) for r in snapshot["records"]) == 2
        assert not checkpoints and not errors
        print(json.dumps({"passed": True, "accepted_http_requests": len(accepted),
            "retained_results": len(app.data_cache), "pending_images": 4, "ui_ticks": ticks,
            "results_on_disk": 2, "callback_errors": errors}))
    finally:
        release.set()
        worker.join(5)
        app.destroy()
        server.shutdown()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pixtagra-cancel-") as path:
        run(Path(path))
