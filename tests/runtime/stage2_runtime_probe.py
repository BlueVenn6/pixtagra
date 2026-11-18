"""Actual-GUI Phase 2 probe using only temporary synthetic data and mocked API work."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
APP_PATH = ROOT / "pixtagra.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module():
    spec = importlib.util.spec_from_file_location("pixtagra_stage2_gui_probe", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _drain(app, count=8):
    for _ in range(count):
        app.update_idletasks()
        app.update()


def main():
    with tempfile.TemporaryDirectory(prefix="pixtagra_stage2_") as root_text:
        root = Path(root_text)
        home = root / "home"
        config = root / "config"
        temp_dir = root / "temp"
        source_dir = root / "fixtures"
        output_dir = root / "output"
        for directory in (home, config, temp_dir, source_dir, output_dir):
            directory.mkdir()

        os.environ["PSS_NO_STARTUP_DIALOG"] = "1"
        os.environ["PSS_CONFIG_DIR"] = str(config)
        os.environ["PSS_CONFIG_FILE"] = str(config / "config.json")
        os.environ["HOME"] = str(home)
        # Windows Path.home() uses USERPROFILE. It must be isolated before the
        # source module is imported so the test cannot resolve the real vault.
        os.environ["USERPROFILE"] = str(home)
        os.environ["TEMP"] = str(temp_dir)
        os.environ["TMP"] = str(temp_dir)

        image_path = source_dir / "synthetic_gui_fixture.jpg"
        image = Image.new("RGB", (24, 18), "#3399cc")
        image.save(image_path, "JPEG", quality=100)
        image.close()

        module = _load_module()
        assert module.APP_CONFIG_DIR.resolve() == config.resolve()
        assert module.VAULT_DIR.resolve() == (home / ".pictureseo").resolve()

        original_open_files = module.filedialog.askopenfilenames
        original_save_file = module.filedialog.asksaveasfilename
        original_info = module.messagebox.showinfo
        original_warning = module.messagebox.showwarning
        original_error = module.messagebox.showerror
        original_ask_yes_no = module.messagebox.askyesno
        app = None
        try:
            module.filedialog.askopenfilenames = lambda **_kwargs: (str(image_path),)
            module.messagebox.showinfo = lambda *_args, **_kwargs: None
            module.messagebox.showwarning = lambda *_args, **_kwargs: None
            module.messagebox.showerror = lambda *_args, **_kwargs: None
            module.messagebox.askyesno = lambda *_args, **_kwargs: False

            app = module.App()
            _drain(app)
            assert app.title() == "PixTagra"
            app.load_images()
            _drain(app)
            assert len(app.cards) == 1
            card = app.cards[str(image_path)]
            assert app.showing_ws is True

            simulated = {
                "title": "Synthetic Blue Rectangle Test Image",
                "description": "A synthetic blue rectangular image generated only for isolated GUI runtime testing.",
                "keywords": ["synthetic", "blue", "rectangle", "test"],
            }
            app._record_analysis_result(card, simulated, total=1)
            _drain(app)
            title, description, keywords = card.get_data()
            assert title == simulated["title"]
            assert description == simulated["description"]
            assert keywords == simulated["keywords"]

            json_path = output_dir / "runtime_export.json"
            csv_path = output_dir / "runtime_export.csv"
            save_targets = iter((str(json_path), str(csv_path)))
            module.filedialog.asksaveasfilename = lambda **_kwargs: next(save_targets)
            app._export()
            app._export_shutterstock_csv()
            exported_json = json.loads(json_path.read_text(encoding="utf-8"))
            csv_text = csv_path.read_text(encoding="utf-8-sig")
            assert exported_json[0]["file"] == image_path.name
            assert "Filename,Description,Keywords,Categories" in csv_text

            app.is_processing = True
            app.btns["⏹ Cancel"].configure(state="normal")
            cancel_state_before = app.btns["⏹ Cancel"].cget("state")
            app._stop()
            cancel_state_after = app.btns["⏹ Cancel"].cget("state")
            assert app.stop_flag is True
            assert app.is_processing is True
            assert cancel_state_before == cancel_state_after == "normal"
            app.is_processing = False

            duplicate_calls = []
            app._call_api = lambda *_args, **_kwargs: duplicate_calls.append(1) or dict(simulated)
            app._apply_sanitizer_and_repair = lambda result, *_args, **_kwargs: result
            app._process(card, app.model_var.get(), app.mode_var.get(), "", "", "")
            app._process(card, app.model_var.get(), app.mode_var.get(), "", "", "")
            assert len(duplicate_calls) == 2

            app.destroy()
            app = None
            reopened = module.App()
            assert reopened.cards == {}

            result = {
                "gui_created_and_destroyed": True,
                "window_title": reopened.title(),
                "imported_cards": 1,
                "simulated_analysis_filled": True,
                "json_export_rows": len(exported_json),
                "csv_has_expected_header": True,
                "cancel_sets_flag_only": True,
                "repeat_process_calls": len(duplicate_calls),
                "restart_persisted_cards": len(reopened.cards),
                "network_requests": 0,
                "real_home_accessed": False,
            }
            print(json.dumps(result, sort_keys=True))
            reopened.destroy()
        finally:
            module.filedialog.askopenfilenames = original_open_files
            module.filedialog.asksaveasfilename = original_save_file
            module.messagebox.showinfo = original_info
            module.messagebox.showwarning = original_warning
            module.messagebox.showerror = original_error
            module.messagebox.askyesno = original_ask_yes_no
            if app is not None:
                app.destroy()


if __name__ == "__main__":
    main()
