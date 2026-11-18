"""Isolated real-widget acceptance of batch state, file exports and DPI sizing.

Only the provider boundary is simulated. Queues, controls, editors, export and
image metadata writers are production code; no real key or paid request is used.
"""
import copy
import csv
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace


def run_probe(module, output_dir):
    if not module._packaged_test_isolation_enabled():
        raise RuntimeError("UI probe requires explicit test isolation")
    from PIL import Image
    from tkinter import font as tkfont
    from pixtagra_localization import translate
    from pixtagra_language import normalize_language

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    module.APP_CONFIG_DIR = output / "config"
    module.CONFIG_FILE = module.APP_CONFIG_DIR / "config.json"
    original_dialogs = {name: getattr(module.messagebox, name) for name in ("showinfo", "showwarning", "showerror", "askyesno")}
    original_files = {name: getattr(module.filedialog, name) for name in ("askopenfilenames", "asksaveasfilename", "askdirectory")}
    dialogs, errors, checks = [], [], []
    for name in original_dialogs:
        setattr(module.messagebox, name, lambda *a, _name=name, **kw: dialogs.append((_name, a)) or (_name == "askyesno" and a[0] in {translate("Keyword quality review"), translate("Compliance review required")}))
    metadata = {
        "title": "Mountain and Lake Landscape beneath Clouds in Evening Light",
        "description": "Mountains rise above a lake with trees along the water and clouds overhead. Evening light falls across the landscape, showing the rocky slopes and the still water in a wide view.",
        "keywords": ["mountain", "lake", "forest", "water", "clouds", "sky", "grass", "trees", "rocks", "shoreline", "reflection", "valley", "peak", "meadow", "wilderness", "nature", "outdoors", "scenery", "panorama", "horizon", "daylight", "sunlight", "summer", "spring", "green", "blue", "white", "terrain", "slope", "ridge", "foliage", "evergreen", "rural", "vista", "landscape"],
    }
    app = module.App()
    language = normalize_language(os.environ.get("PIXTAGRA_PROBE_LANGUAGE", "en"))
    app._on_language_change("简体中文" if language == "zh" else "English")
    app.title("PixTagra — isolated UI acceptance — synthetic images / no API")
    def callback_error(*exc):
        errors.append(str(exc[1]))
        print(f"UI callback error: {exc[1]}", flush=True)
    app.report_callback_exception = callback_error
    app.clipboard_get = lambda: "synthetic clipboard"
    app.model_var.set("Local Ollama")
    app._on_model_change()
    app.visual_review_var.set(False)
    scale = os.environ.get("PIXTAGRA_PROBE_SCALE")
    if scale:
        factor = float(scale) / module.ctk.ScalingTracker.get_window_dpi_scaling(app)
        module.ctk.set_widget_scaling(factor)
        module.ctk.set_window_scaling(factor)
    gate, work_gate = threading.Event(), threading.Event()
    gate.set()
    work_gate.set()
    def smoke(*args, **kw):
        if not gate.wait(20):
            raise TimeoutError("probe preflight gate")
        return True, "synthetic success", {}
    def process(*args, **kw):
        if not work_gate.wait(20):
            raise TimeoutError("probe generation gate")
        return copy.deepcopy(metadata)
    app._run_request_smoke_test = smoke
    app._process = process
    def pump(until=lambda: True):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            app.update()
            if until():
                return
            time.sleep(.005)
        raise TimeoutError(f"UI acceptance condition timed out; callback errors={errors}; status={app.prog_lbl.cget('text')}")
    def check(name, condition):
        checks.append({"name": name, "passed": bool(condition)})
        print(f"{name}: {bool(condition)}".encode("ascii", "backslashreplace").decode("ascii"), flush=True)
    def status_source():
        source = app.prog_lbl._ui_text_source
        check("status:matches_selected_language", app.prog_lbl.cget("text") == translate(source, language))
        return source
    def load(number):
        path = output / f"fixture-{number}.png"
        Image.new("RGB", (640, 480), (50, 100 + number * 20, 160)).save(path)
        module.filedialog.askopenfilenames = lambda **kw: [str(path)]
        app.load_images()
        pump(lambda: not app.is_importing)
        return app.cards[str(path)]
    def exports(stem, expected):
        expected_rows = {Path(path).name: copy.deepcopy(card._cached_data) for path, card in app.cards.items() if card._cached_data.get("title")}
        for key, suffix in (("📄 JSON", "all.json"), ("📄 JSON ready", "ready.json"), ("📊 SS CSV", "stock.csv")):
            path = output / f"{stem}-{suffix}"
            module.filedialog.asksaveasfilename = lambda **kw: str(path)
            app.btns[key].invoke()
            check(f"{stem}:{suffix}:exists", path.is_file())
            if path.is_file():
                if suffix.endswith("json"):
                    rows = json.loads(path.read_text(encoding="utf-8"))
                    check(f"{stem}:{suffix}:content", len(rows) == expected and all(row["title"] == expected_rows[row["file"]]["title"] and row["keywords"] == expected_rows[row["file"]]["keywords"] for row in rows))
                else:
                    with path.open(encoding="utf-8-sig", newline="") as stream:
                        rows = list(csv.DictReader(stream))
                    check(f"{stem}:{suffix}:content", len(rows) == expected and metadata["description"] in rows[0]["Description"] and "mountain" in rows[0]["Keywords"])
    try:
        first = load(1)
        pump()
        check("empty:csv_disabled", app.btns["📊 SS CSV"].cget("state") == "disabled")
        app.btns["⚡ Analyze"].invoke()
        pump(lambda: not app.is_processing and app.stats["processed"] == 1)
        pump()
        check("first:final_label", status_source().startswith("Done:"))
        exports("first", 1)
        first.var_sel.set(False)
        second = load(2)
        gate.clear()
        work_gate.clear()
        app.btns["⚡ Analyze"].invoke()
        pump(lambda: app._network_check_busy)
        check("second:preflight_zero", app.prog.get() == 0 and app.stats["processed"] == 0)
        check("second:preflight_label", app.prog_lbl.cget("text") == translate("Checking model connection...", language))
        for key in ("💾 Save", "💾 Save ready", "💾 Save all", "📁 Save→Folder", "📄 JSON", "📄 JSON ready", "📊 SS CSV", "+ Add", "⌂ Home"):
            check(f"busy:{key}:disabled", app.btns[key].cget("state") == "disabled")
        app._focus_card(second)
        pump()
        check("busy:card_actions_disabled", all(button.cget("state") == "disabled" for button in (second.view.regen_btn, second.view.undo_btn, second.view.remove_btn)))
        gate.set()
        pump(lambda: not app._network_check_busy)
        check("second:processing_zero", app.prog.get() == 0)
        work_gate.set()
        pump(lambda: not app.is_processing)
        pump()
        check("second:final_state", app.stats["processed"] == 1 and status_source().startswith("Done:"))
        exports("second", 2)
        # A failed preflight must not report the previous batch's successes.
        third = load(3)
        first.var_sel.set(False)
        second.var_sel.set(False)
        app._run_request_smoke_test = lambda *a, **kw: (False, "synthetic offline", {})
        app._format_connection_feedback = lambda result, text: text
        app.btns["⚡ Analyze"].invoke()
        pump(lambda: not app.is_processing)
        check("failed_preflight:honest_state", app.prog.get() == 0 and "not started" in status_source().lower() and app.stats["success"] == 0)
        # Cancel while checking connection; retain all previous results.
        app._run_request_smoke_test = smoke
        gate.clear()
        app.btns["⚡ Analyze"].invoke()
        pump(lambda: app._network_check_busy)
        app.btns["⏹ Cancel"].invoke()
        gate.set()
        pump(lambda: not app.is_processing)
        check("cancel:honest_state", app.prog.get() == 0 and status_source().startswith("Cancelled:"))
        check("cancel:previous_results_retained", all(card._cached_data["title"] == metadata["title"] for card in (first, second)))
        # Exercise a real posted native edit menu at the actual widget scale.
        app._focus_card(first)
        pump()
        target = first.view.title_e
        target.clipboard_get = lambda: "synthetic clipboard"
        # On Windows tk_popup is a native modal loop. The desktop test driver
        # dismisses it with Escape after observing this marker and the menu.
        noninteractive = os.environ.get("PIXTAGRA_UI_PROBE_NONINTERACTIVE") == "1"
        print("menu:construction_only" if noninteractive else "menu:posting_native_popup", flush=True)
        original_popup = module.tk.Menu.tk_popup
        try:
            if noninteractive:
                module.tk.Menu.tk_popup = lambda *a, **kw: None
            app._show_edit_context_menu(SimpleNamespace(widget=target, x_root=target.winfo_rootx()+20, y_root=target.winfo_rooty()+20))
        finally:
            module.tk.Menu.tk_popup = original_popup
        pump()
        menu = app._edit_context_menu
        check("menu:matches_selected_language", menu.entrycget(0, "label") == translate("Cut", language)
              and menu.entrycget(1, "label") == translate("Copy", language))
        menu_font = tkfont.Font(root=app, font=menu.cget("font"))
        editor_font = tkfont.Font(root=app, font=target.cget("font"))
        sizing = {"scale": app.workspace._get_widget_scaling(), "menu_linespace": menu_font.metrics("linespace"), "editor_linespace": editor_font.metrics("linespace"), "menu_width": menu.winfo_reqwidth(), "menu_height": menu.winfo_reqheight()}
        check("menu:font_proportion", sizing["menu_linespace"] >= sizing["editor_linespace"] * .85)
        check("menu:no_selection_disables_cut_copy", str(menu.entrycget(0, "state")) == str(menu.entrycget(1, "state")) == "disabled")
        menu.unpost()
        dialog_geometry = {}
        def inspect_dialog(open_dialog, name):
            deadline = time.monotonic() + 3
            def inspect():
                dialogs_now = [child for child in app.winfo_children()
                               if isinstance(child, (module.tk.Toplevel, module.ctk.CTkToplevel))]
                if not dialogs_now:
                    if time.monotonic() < deadline:
                        app.after(20, inspect)
                        return
                    errors.append(f"{name}: no dialog")
                    return
                dialog = dialogs_now[-1]
                dialog.update_idletasks()
                controls = []
                def visit(widget):
                    if isinstance(widget, module.tk.Button):
                        controls.append(widget)
                    for child in widget.winfo_children():
                        visit(child)
                visit(dialog)
                if time.monotonic() < deadline and (
                    not dialog.winfo_ismapped() or not controls
                    or any(button.winfo_width() <= 1 or button.winfo_height() <= 1 for button in controls)
                ):
                    app.after(20, inspect)
                    return
                app.after(1000, lambda: dialog.destroy() if dialog.winfo_exists() else None)
                dialog_geometry[name] = {"size": [dialog.winfo_width(), dialog.winfo_height()],
                    "buttons": [{"label": button.cget("text"),
                                 "font_linespace": tkfont.Font(root=app, font=button.cget("font")).metrics("linespace"),
                                 "bounds": [button.winfo_rootx()-dialog.winfo_rootx(), button.winfo_rooty()-dialog.winfo_rooty(), button.winfo_width(), button.winfo_height()]}
                                for button in controls]}
                check(f"dialog:{name}:buttons_fit", bool(controls) and all(
                    button.winfo_rootx() >= dialog.winfo_rootx()
                    and button.winfo_rootx()+button.winfo_width() <= dialog.winfo_rootx()+dialog.winfo_width()+2
                    and button.winfo_rooty()+button.winfo_height() <= dialog.winfo_rooty()+dialog.winfo_height()+2
                    for button in controls))
                check(f"dialog:{name}:readable_font", all(tkfont.Font(root=app, font=button.cget("font")).metrics("linespace") >= sizing["editor_linespace"] * .70 for button in controls))
                if language == "zh":
                    import re
                    check(f"dialog:{name}:chinese_actions", bool(controls) and all(
                        not re.search(r"[A-Za-z]", str(button.cget("text"))) for button in controls))
                dialog.destroy()
            app.after(100, inspect)
            open_dialog()
        inspect_dialog(lambda: app._confirm_batch_retry([first]), "retry")
        inspect_dialog(lambda: app._show_quality_recovery_dialog([(first, "synthetic review", False, "review details")], 1, lambda: None, lambda: None, lambda: None), "quality")
        inspect_dialog(first.view._confirm_regen_scope, "manual_regeneration")
        inspect_dialog(first.view._open_keyword_editor, "keyword_editor")
        # Real image copy/save; production writer verifies embedded metadata,
        # then independently reopen the exact output and verify it again.
        third.var_sel.set(False)
        first.var_sel.set(True)
        second.var_sel.set(False)
        app._update_stats()
        module.filedialog.askdirectory = lambda **kw: str(output)
        saved_keywords = list(first._cached_data["keywords"])
        app.btns["📁 Save→Folder"].invoke()
        pump(lambda: not app.is_saving)
        copies = list(output.glob("SEO_Ready_*/*.png"))
        check("save:exact_copy_created", len(copies) == 1)
        if copies:
            module.metadata._verify_png_written_metadata(str(copies[0]), metadata["title"], metadata["description"], saved_keywords, "", source_png_path=first.path)
            with Image.open(copies[0]) as saved:
                saved.load()
                check("save:reopened_pixels", saved.size == (640, 480))
        check("save:only_saved_card_removed", first.path not in app.cards and second.path in app.cards and third.path in app.cards)
        app.btns["💾 Save ready"].invoke()
        pump(lambda: not app.is_saving)
        check("save_ready:removes_only_ready", second.path not in app.cards and third.path in app.cards)
        third.fill(copy.deepcopy(metadata))
        third.var_sel.set(True)
        app._update_stats()
        app.btns["💾 Save"].invoke()
        pump(lambda: not app.is_saving)
        check("save_selected:completed", third.path not in app.cards)
        fourth, fifth = load(4), load(5)
        fourth.fill(copy.deepcopy(metadata))
        fifth.fill(copy.deepcopy(metadata))
        app._update_stats()
        app.btns["💾 Save all"].invoke()
        pump(lambda: not app.is_saving)
        check("save_all:completed", not app.cards and not app.showing_ws)
        saved_copies = [path for path in output.glob("*.png") if not path.name.startswith("fixture-")]
        check("save_buttons:real_image_files", len(saved_copies) == 4)
        for path in saved_copies:
            module.metadata._verify_png_written_metadata(str(path), metadata["title"], metadata["description"], saved_keywords)
        # Exercise all three delivery states in the actual packaged widgets.
        ready_card, review_card, failed_card = load(6), load(7), load(8)
        ready_card.fill(copy.deepcopy(metadata))
        review_card.fill({**copy.deepcopy(metadata), "keyword_policy_version": 3,
                          "keywords": metadata["keywords"][:19]})
        app._mark_processing_failed(failed_card)
        app._update_stats()
        check("quality:three_states", app._batch_counts()[:4] == (1, 1, 1, 0))
        check("quality:under_twenty_blocks_platform_export", app._csv_export_counts()[1] == 1
              and app.btns["📊 SS CSV"].cget("state") == "disabled")
        review_card.fill({**copy.deepcopy(metadata), "keyword_policy_version": 3,
                          "keyword_review": {"status": "unavailable"}})
        app._update_stats()
        check("quality:incomplete_review_is_not_transport_failure", app._batch_counts()[:4] == (1, 1, 1, 0))
        review_card.mark_saved()
        app._update_stats()
        check("quality:saved_warning_is_not_ready", app._batch_counts()[:4] == (1, 1, 1, 0)
              and review_card.status_text == "● Saved — needs review")
        check("callbacks:no_errors", not errors)
        return {"passed": all(item["passed"] for item in checks), "checks": checks, "sizing": sizing, "dialog_geometry": dialog_geometry, "callback_errors": errors, "external_api_requests": 0, "ui_language": language, "metadata_fixture_language": "en", "provider_boundary": "simulated; production queues, GUI and file writers", "live_provider_verified": False, "native_menu_posted": not noninteractive}
    finally:
        gate.set()
        work_gate.set()
        app.destroy()
        for name, fn in original_dialogs.items():
            setattr(module.messagebox, name, fn)
        for name, fn in original_files.items():
            setattr(module.filedialog, name, fn)


if __name__ == "__main__":
    import sys
    os.environ["PIXTAGRA_PACKAGED_TEST_ISOLATION"] = "1"
    os.environ["PSS_NO_STARTUP_DIALOG"] = "1"
    os.environ["PSS_CONFIG_DIR"] = str(Path(sys.argv[1]).resolve() / "initial-config")
    import pixtagra
    result = run_probe(pixtagra, sys.argv[1])
    (Path(sys.argv[1]) / "ui-acceptance.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
