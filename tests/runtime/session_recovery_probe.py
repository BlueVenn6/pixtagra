"""Run two real GUI processes with an abrupt exit between them, in an isolated home."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def child(phase, folder):
    os.environ["PIXTAGRA_PACKAGED_TEST_ISOLATION"] = "1"
    os.environ["PSS_NO_STARTUP_DIALOG"] = "1"
    os.environ["PSS_CONFIG_DIR"] = str(folder)
    os.environ["PSS_CONFIG_FILE"] = str(folder / "config.json")
    from PIL import Image
    import pixtagra as m
    dialogs = []
    m.messagebox.showwarning = lambda *args: dialogs.append(args)
    app = m.App()
    app.title("PixTagra — isolated recovery verification")
    errors = []
    app.report_callback_exception = lambda *exc: errors.append(str(exc[1]))
    path = str(folder / "image.png")
    if phase == "write":
        Image.new("RGB", (200, 100), "blue").save(path)
        record = m.CardRecord(path, app)
        app.cards[path] = record
        app.card_order.append(path)
        app._resume_session()
        app.update()
        record.view.title_e.insert(0, "Manual title survives an unexpected application exit")
        record.view._sync_now()
        record.var_sel.set(False)
        record.regen_scope = "unmodified"
        assert app._persist_session()
        app._checkpoint_result(record, {"title": "Generated title", "description": "Paid description",
            "keywords": ["blue", "rectangle", "background", "shape", "horizontal"]}, "review", "Test Model")
        os._exit(0)  # No App.destroy, no Tk close callback, no final autosave.
    record = app.cards[path]
    assert not app.showing_ws and not app.is_processing
    assert not record.var_sel.get()
    assert record._cached_data["title"] == "Manual title survives an unexpected application exit"
    assert record._cached_data["description"] == "Paid description"
    assert record._cached_data["keyword_review"]["stage"] == "review"
    assert record.manual_fields == {"title"}
    assert record.last_generated_model == "Test Model"
    app._resume_session()
    app.update()
    assert record.view.title_e.get() == record._cached_data["title"]
    assert not dialogs and not errors
    app._request_close()
    print(json.dumps({"passed": True, "real_process_restart": True, "paid_result_retained": True,
        "manual_title_retained": True, "selection_retained": True, "review_stage_retained": True,
        "no_automatic_paid_requests": True, "callback_errors": errors}))


if __name__ == "__main__":
    if len(sys.argv) == 3:
        child(sys.argv[1], Path(sys.argv[2]))
    else:
        with tempfile.TemporaryDirectory(prefix="pixtagra-recovery-") as folder:
            for phase in ("write", "read"):
                subprocess.run([sys.executable, __file__, phase, folder], check=True)
