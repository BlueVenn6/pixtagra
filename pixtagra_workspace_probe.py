"""Opt-in GUI regression probe; synthetic images, no provider or credential access."""

from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import shutil
import tempfile
import time
import threading
import hashlib
import math


# Project acceptance budgets on the recorded machine/DPI, not an FPS claim.
# Total timings also include the probe's 20/35 ms event-pump wait and async work.
SCROLL_BUDGET_MS = {
    "wheel_paint_ms_p95": 50, "wheel_paint_ms_max": 150,
    "scroll_paint_ms_p95": 150, "scroll_paint_ms_max": 250,
    "wheel_ms_p95": 100, "wheel_ms_max": 250,
    "scroll_ms_p95": 200, "scroll_ms_max": 350,
}


def assess_scroll_performance(result):
    """A functional pass must not silently authorize a slow release."""
    failures = [name for name, limit in SCROLL_BUDGET_MS.items()
                if not isinstance(result.get(name), (int, float))
                or not math.isfinite(result[name]) or not 0 <= result[name] <= limit]
    if result.get("loaded", 0) < 500 or len(set(result.get("sample_images", []))) < 500:
        failures.append("500_distinct_photos_required")
    if result.get("keywords_per_record", 0) < 35:
        failures.append("full_metadata_required")
    if result.get("wheel_moves", 0) < 90 or result.get("scroll_cycles", 0) < 5:
        failures.append("insufficient_scroll_samples")
    if not result.get("passed") or result.get("screenshot_error"):
        failures.append("functional_or_visual_check_failed")
    return {"passed": not failures, "failures": failures, "budgets_ms": dict(SCROLL_BUDGET_MS)}


def p95(values):
    return sorted(values)[math.ceil(len(values) * .95) - 1]


def read_corpus_manifest(path):
    """Validate input identity before either source or packaged GUI testing."""
    path = Path(path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    images = []
    for record in manifest["images"]:
        image = (path.parent / record["file"]).resolve()
        if not image.is_relative_to(path.parent):
            raise ValueError("Corpus image escaped its manifest directory")
        if hashlib.sha256(image.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Corpus image changed: " + image.name)
        images.append(str(image))
    if len(images) != len(set(images)) or not images:
        raise ValueError("Corpus contains duplicate paths or no images")
    pixel_hashes = [r["pixels_sha256"] for r in manifest["images"]]
    if len(set(pixel_hashes)) != len(images):
        raise ValueError("Corpus contains repeated decoded images")
    return images


def run_probe(module, output_dir, count=150, scale=None, sample_images=None, width=None, cycles=5):
    from PIL import Image, ImageDraw, ImageGrab

    if not module._packaged_test_isolation_enabled():
        raise RuntimeError("Workspace probe requires explicit test isolation")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    callbacks = []
    dialogs = []
    original_dialogs = {}
    # --scale is the effective DPI factor, not an extra multiplier on top of
    # Windows DPI (1.5 * 225% used to test 337.5% while reporting 150%).
    module.ctk.set_widget_scaling(1.0)
    module.ctk.set_window_scaling(1.0)
    with tempfile.TemporaryDirectory(prefix="pixtagra-workspace-") as scratch:
        root = Path(scratch)
        module.CONFIG_FILE = root / "config.json"
        module.APP_CONFIG_DIR = root
        module.VAULT_DIR = root / "unused-vault"
        module.VAULT_FILE = module.VAULT_DIR / "keys.vault"
        module.SALT_FILE = module.VAULT_DIR / "keys.salt"
        paths = []
        for index in range(count):
            if sample_images:
                source = Path(sample_images[index % len(sample_images)])
                path = root / f"image-{index:03d}{source.suffix}"
                shutil.copyfile(source, path)
                paths.append(str(path))
                continue
            path = root / f"image-{index:03d}.jpg"
            with Image.new("RGB", (1200, 600), "#208090") as img:
                draw = ImageDraw.Draw(img)
                draw.rectangle((0, 0, 200, 599), fill="red")
                draw.rectangle((1000, 0, 1199, 599), fill="blue")
                draw.text((550, 270), str(index), fill="white", font_size=60)
                img.save(path, quality=90)
            paths.append(str(path))
        original_open = module.filedialog.askopenfilenames
        module.filedialog.askopenfilenames = lambda **kwargs: paths
        for name in ("showinfo", "showwarning", "showerror"):
            original_dialogs[name] = getattr(module.messagebox, name)
            setattr(module.messagebox, name, lambda *args, **kwargs: dialogs.append(args))
        app = module.App()
        app.title("PixTagra — UI stress test: temporary copies / synthetic text / no API")
        native_scale = app.workspace._get_widget_scaling()
        scale = native_scale if scale is None else scale
        module.ctk.set_widget_scaling(scale / native_scale)
        module.ctk.set_window_scaling(scale / native_scale)
        app._center_main_window()
        if width is not None:
            app.minsize(min(800, width), 600)
            app.geometry(f"{width}x700")
        app.report_callback_exception = lambda *exc: callbacks.append(str(exc[1]))
        app.deiconify()
        visibility_events = []
        def visibility_event(event):
            if event.widget is app:
                visibility_events.append({"event": str(event.type), "state": app.state(),
                                          "time": round(time.monotonic(), 3)})
        app.bind("<Unmap>", visibility_event, add="+")
        app.bind("<Map>", visibility_event, add="+")

        def pump(seconds=0.06):
            end = time.perf_counter() + seconds
            while time.perf_counter() < end:
                app.update()
                time.sleep(0.003)

        try:
            pump(0.2)
            start = time.perf_counter()
            app.load_images()
            pump(0.6)
            deadline = time.perf_counter() + 120
            while getattr(app, "is_importing", False) and time.perf_counter() < deadline:
                pump(0.05)
            if app.is_importing:
                raise TimeoutError("Image import exceeded 120 seconds; scrolling was not tested")
            import_seconds = time.perf_counter() - start
            loaded = len(app.cards)
            initial_thumbnails = sum(record.thumb_image is not None for record in app.cards.values())
            initial_summary = app.batch_summary_lbl.cget("text")
            # Distinct persisted values expose cross-record contamination on recycling.
            # Full-length fields reproduce the cost of editing generated metadata.
            workload_keywords = [
                "landscape", "mountain", "lake", "forest", "water", "clouds", "sky",
                "grass", "trees", "rocks", "shoreline", "reflection", "valley", "peak",
                "meadow", "wilderness", "nature", "outdoors", "scenery", "panorama",
                "horizon", "daylight", "sunlight", "summer", "spring", "green", "blue",
                "white", "terrain", "slope", "ridge", "foliage", "evergreen", "rural", "vista",
            ]
            expected = {}
            for index, record in enumerate(app.cards.values()):
                title = f"Distinct title for photograph number {index} in this batch"
                record._cached_data = {"title": title,
                    "description": "This is synthetic text for the interface workload test, with a full length description matching generated metadata. It checks that every image keeps its own editable fields during scrolling, selection, export, and recovery.",
                    "keywords": list(workload_keywords)}
                record.saved = index % 3 == 0
                expected[record.path] = title
                record.refresh_view(force_fields=True)
            pump()
            before = {v.record.path: v for v in app.card_views if v.record is not None}
            pitch = app._card_height + app._card_gap
            height = len(app.card_order) * pitch + 20
            app.canvas.yview_moveto((2 * pitch + 20) / height)
            pump()
            after = {v.record.path: v for v in app.card_views if v.record is not None}
            overlap = set(before) & set(after)
            rebound_overlap = sum(before[p] is not after[p] for p in overlap)
            timings = []
            paint_timings = []
            mapping_errors = []
            hidden_samples = 0
            for fraction in [0.0, 0.2, 0.6, 0.99, 0.5, 0.0] * cycles:
                start = time.perf_counter()
                app.canvas.yview_moveto(fraction)
                app.update_idletasks()
                paint_timings.append((time.perf_counter() - start) * 1000)
                pump(0.035)
                hidden_samples += not bool(app.workspace.winfo_ismapped())
                timings.append((time.perf_counter() - start) * 1000)
                for view in app.card_views:
                    if view.record and view.title_e.get() != expected[view.record.path]:
                        mapping_errors.append(Path(view.record.path).name)
            pump(0.3)
            wheel_timings = []
            wheel_paint_timings = []
            wheel_moves = 0
            wheel_direction = -120
            editor_visibility_events = []
            editor_bindings = []
            for view in app.card_views:
                for editor in (view.title_e, view.desc_e, view.kw_e):
                    for event_name in ("<Map>", "<Unmap>"):
                        binding = editor.bind(event_name, lambda event: editor_visibility_events.append(str(event.type)), add="+")
                        editor_bindings.append((editor, event_name, binding))
            for _ in range(90):
                start = time.perf_counter()
                # Pointer movement / another window can deliver Leave events.
                # Each sample explicitly targets the canvas and must move it.
                app._canvas_mousewheel_active = True
                previous_yview = app.canvas.yview()
                if previous_yview[1] >= 1.0:
                    wheel_direction = 120
                elif previous_yview[0] <= 0:
                    wheel_direction = -120
                app._on_mousewheel(type("Wheel", (), {"delta": wheel_direction, "widget": app.canvas})())
                app.update_idletasks()
                wheel_paint_timings.append((time.perf_counter() - start) * 1000)
                pump(0.02)
                wheel_moves += app.canvas.yview() != previous_yview
                wheel_timings.append((time.perf_counter() - start) * 1000)
            for editor, event_name, binding in editor_bindings:
                editor.unbind(event_name, binding)
            # Exercise continuous thumb motion, including a three-image batch.
            # Prior probes jumped then waited; they did not cover drag updates.
            drag_errors = []
            drag_paint = []
            motion_capture_errors = []
            for sample, step in enumerate(list(range(31)) + list(range(30, -1, -1))):
                started = time.perf_counter()
                app._on_scrollbar("moveto", step / 30)
                app.update_idletasks()
                drag_paint.append((time.perf_counter() - started) * 1000)
                if not app.workspace.winfo_ismapped():
                    drag_errors.append("workspace hidden")
                for view in app.card_views:
                    if view.record and view.title_e.get() != expected[view.record.path]:
                        drag_errors.append("wrong editor content")
                pump(0.016)
                if sample in (7, 30, 50):
                    try:
                        import ctypes
                        hwnd = ctypes.windll.user32.GetParent(app.winfo_id())
                        ImageGrab.grab(window=hwnd).save(output / f"drag-{sample:02d}.png")
                    except OSError as exc:
                        motion_capture_errors.append(str(exc))
            # Slow provider I/O must leave Tk's event loop responsive.
            responsive_ticks = []
            network_result = []
            release = threading.Event()
            worker_ids = []
            def slow_request():
                worker_ids.append(threading.get_ident())
                release.wait(2)
                return "ok"
            if hasattr(app, "_run_network_check"):
                app._run_network_check(slow_request, lambda value: network_result.append(value))
                app.after(10, lambda: responsive_ticks.append(True))
                pump(0.1)
                release.set()
                pump(0.15)
            corrupt_titles = [Path(p).name for p, record in app.cards.items()
                              if record._cached_data["title"] != expected[p]]
            editable = next(record for record in app.cards.values() if not record.saved)
            for record in app.cards.values():
                record.var_sel.set(record is editable or record.saved)
            batch_targets = app._selected_keyword_targets()
            batch_result = app._apply_batch_keywords(batch_targets, ["chinese pattern", "chinese pattern"])
            batch_repeated = app._apply_batch_keywords(batch_targets, ["CHINESE PATTERN"])
            batch_keywords_correct = (
                batch_result == (1, 0, 1) and batch_repeated == (0, 0, 0)
                and editable._cached_data["keywords"].count("chinese pattern") == 1
                and app.data_cache[editable.path]["keywords"] == editable._cached_data["keywords"]
                and all("chinese pattern" not in c._cached_data["keywords"]
                        for c in app.cards.values() if c is not editable)
            )
            app._focus_card(editable)
            pump()
            editor = editable.view
            editor.title_e.delete(0, "end")
            editor.title_e.insert(0, "Manual title survives scrolling and JSON export")
            editor._sync_now()
            app.canvas.yview_moveto(0.99)
            pump()
            app._focus_card(editable)
            pump()
            manual_edit_preserved = editable.view.title_e.get() == "Manual title survives scrolling and JSON export"
            export_current = app.data_cache[editable.path]["title"] == editable.view.title_e.get()
            original_title = editable.get_data()[0]
            for _ in range(2):
                editable.regen_scope = "unmodified"
                editable.fill({"title": "Automatic title must not replace manual text", "description": "Regenerated description.", "keywords": ["mountain", "lake"]})
            repeat_generation_preserves_manual = (
                editable.get_data()[0] == original_title and "title" in editable.manual_fields
                and "keywords" in editable.manual_fields
                and "chinese pattern" in editable.get_data()[2]
                and app.data_cache[editable.path]["title"] == original_title
            )
            editable.view.undo_btn.invoke()
            editable.view.undo_btn.invoke()
            undo_restores_manual = editable.get_data()[0] == original_title and "title" in editable.manual_fields

            initial_model = app.model_var.get()
            app.model_var.set("OpenAI GPT-5.5")
            app._on_model_change()
            app.base_url_var.set("https://relay.example.test/v1")
            app.model_var.set("Moonshot Kimi")
            app._on_model_change()
            other_provider_uses_direct = app.base_url_var.get() == ""
            app.model_var.set("Local Ollama")
            app._on_model_change()
            app.model_var.set("OpenAI GPT-5.5")
            app._on_model_change()
            provider_routes_isolated = other_provider_uses_direct and app.base_url_var.get() == "https://relay.example.test/v1"
            app.model_var.set(initial_model)
            app._on_model_change()
            # Model changes schedule responsive layout. Inspect settled geometry.
            # Expand both panels: every existing action must remain reachable
            # and unclipped even though optional actions now start collapsed.
            for group, row in app.__dict__.get("_workspace_options", {}).items():
                if not row.winfo_manager():
                    app._toggle_workspace_options(group)
            pump(0.15)
            # Resize the same live editor pool, then restore the initial size.
            # Separate fresh-window probes alone miss Configure-time regressions.
            initial_width = round(app.winfo_width() / scale)
            initial_height = round(app.winfo_height() / scale)
            resize_samples = []
            resize_errors = []
            for logical_width in (initial_width + 160, initial_width):
                app.geometry(f"{logical_width}x{initial_height}")
                pump(0.2)
                for view in app.card_views:
                    if not view.record:
                        continue
                    for name in ("regen_btn", "undo_btn", "remove_btn", "kw_expand_btn"):
                        button = getattr(view, name)
                        if button.winfo_x() + button.winfo_width() > view.main.winfo_width() + 2:
                            resize_errors.append(name)
                resize_samples.append({"requested_width": logical_width,
                                       "actual_width": app.winfo_width(), "height": app.winfo_height()})
            visible = [v for v in app.card_views if v.record and 0 <= v.winfo_y() < app.canvas.winfo_height()]
            clipped_actions = []
            for key, button in app.btns.items():
                if not button.winfo_ismapped():
                    clipped_actions.append(key + ": hidden")
                    continue
                if (button.winfo_x() < 0 or button.winfo_x() + button.winfo_width() > button.master.winfo_width() + 2
                        or button.winfo_y() + button.winfo_height() > button.master.winfo_height() + 2
                        or button._text_label.winfo_width() + 2 < button._text_label.winfo_reqwidth()):
                    clipped_actions.append(key + ": clipped")
            for row in app.__dict__.get("_action_rows", []):
                for widget, _preferred in row._items:
                    label = getattr(widget, "_text_label", None)
                    if label is not None and label.winfo_width() + 2 < label.winfo_reqwidth():
                        clipped_actions.append(str(widget.cget("text")) + ": text clipped")
            layout = []
            for view in visible:
                for name in ("regen_btn", "undo_btn", "remove_btn", "kw_expand_btn"):
                    button = getattr(view, name)
                    if (button.winfo_x() + button.winfo_width() > view.main.winfo_width() + 2
                            or button._canvas.winfo_width() > button.winfo_width() + 2
                            or button._text_label.winfo_reqwidth() > button.winfo_width() - 4):
                        clipped_actions.append("card:" + name)
                layout.append({"height": view.winfo_height(),
                               "content_height": view.main.winfo_reqheight(),
                               "description_height": view.desc_e.winfo_height(),
                               "keywords_height": view.kw_e.winfo_height(),
                               "editor_width": view.title_e.winfo_width(),
                               "title_font": str(view.title_e.cget("font")),
                               "thumb_pixels": list(view.record.thumb_image.cget("light_image").size)
                               if view.record.thumb_image else None})
            app.lift()
            pump()
            x, y = app.winfo_rootx(), app.winfo_rooty()
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(app.winfo_id())
            screenshot_error = None
            try:
                ImageGrab.grab(window=hwnd).save(output / "workspace.png")
            except OSError as exc:
                # A disconnected/locked Windows desktop can deny capture. Keep
                # measured behavior available and explicitly report the missing evidence.
                screenshot_error = str(exc)
            thumbnail_view = next(v for v in app.card_views if v.record and v.record.thumb_image is not None)
            saved_thumbnail = thumbnail_view.record.thumb_image
            thumbnail_view.record.thumb_image = None
            thumbnail_view.refresh_from_record()
            thumbnail_handle_cleared = not str(thumbnail_view.img_lbl._label.cget("image"))
            thumbnail_view.record.thumb_image = saved_thumbnail
            thumbnail_view.refresh_from_record()
            result = {"requested": count, "loaded": loaded, "scale": scale,
                      "composited_painting": getattr(app.canvas, "composited_painting", False),
                      "continuous_drag_samples": len(drag_paint),
                      "continuous_drag_errors": drag_errors,
                      "motion_capture_errors": motion_capture_errors,
                      "resize_samples": resize_samples, "resize_errors": resize_errors,
                      "continuous_drag_paint_ms_p95": round(p95(drag_paint), 2),
                      "metadata_source": "synthetic_ui_workload_not_model_output",
                      "keywords_per_record": len(workload_keywords),
                      "scroll_cycles": cycles,
                      "hidden_scroll_samples": hidden_samples,
                      "window_visibility_events": visibility_events[:],
                      "window_state_at_capture": app.state(),
                      "thumbnail_cache_limit": app._thumb_cache_limit,
                      "screenshot_error": screenshot_error,
                      "initial_decoded_thumbnails": initial_thumbnails,
                      "decoded_thumbnails": sum(record.thumb_image is not None for record in app.cards.values()),
                      "sample_images": [Path(p).name for p in sample_images or []],
                      "import_seconds": round(import_seconds, 3), "view_count": len(app.card_views),
                      "overlapping_views_rebound": rebound_overlap,
                      "wrong_title_displays": len(mapping_errors), "corrupt_titles": corrupt_titles,
                      "callback_errors": callbacks, "dialog_count": len(dialogs),
                      "scroll_ms_median": round(statistics.median(timings), 2),
                      "scroll_ms_max": round(max(timings), 2), "layout": layout,
                      "scroll_ms_p95": round(p95(timings), 2),
                      "scroll_paint_ms_p95": round(p95(paint_timings), 2),
                      "scroll_paint_ms_max": round(max(paint_timings), 2),
                      "scroll_samples_ms": [{"paint": round(paint, 2), "total": round(total, 2)}
                                            for paint, total in zip(paint_timings, timings)],
                      "clipped_actions": clipped_actions,
                      "native_dpi_scale": native_scale,
                      "viewport_height": app.canvas.winfo_height(),
                      "wheel_ms_median": round(statistics.median(wheel_timings), 2),
                      "wheel_ms_max": round(max(wheel_timings), 2),
                      "wheel_ms_p95": round(p95(wheel_timings), 2),
                      "wheel_paint_ms_p95": round(p95(wheel_paint_timings), 2),
                      "wheel_paint_ms_max": round(max(wheel_paint_timings), 2),
                      "wheel_samples_ms": [{"paint": round(paint, 2), "total": round(total, 2)}
                                           for paint, total in zip(wheel_paint_timings, wheel_timings)],
                      "wheel_moves": wheel_moves,
                      "editor_visibility_events_during_wheel": len(editor_visibility_events),
                      "thumbnail_handle_cleared": thumbnail_handle_cleared,
                      "network_check_responsive": bool(responsive_ticks) and network_result == ["ok"]
                      and worker_ids != [threading.get_ident()],
                      "manual_edit_preserved": manual_edit_preserved,
                      "batch_keywords_correct": batch_keywords_correct,
                      "repeat_generation_preserves_manual": repeat_generation_preserves_manual,
                      "undo_restores_manual": undo_restores_manual,
                      "provider_routes_isolated": provider_routes_isolated,
                      "export_uses_current_edits": export_current,
                      "unprocessed_summary_correct": "unprocessed" in initial_summary,
                      "effective_widget_scale": app.workspace._get_widget_scaling(),
                      "network_requests": 0, "actual_tk_gui": True}
            # An import callback must not resurrect cleared records.
            app._cancel_workspace_jobs()
            for record in list(app.cards.values()):
                record.destroy()
            app.cards.clear()
            app.card_order.clear()
            app.data_cache.clear()
            app.load_images()
            confirm = module.messagebox.askyesno
            module.messagebox.askyesno = lambda *args, **kwargs: True
            try:
                app._clear()
            finally:
                module.messagebox.askyesno = confirm
            pump(0.15)
            result["clear_during_import_stays_empty"] = not app.cards and not app.is_importing
            result["passed"] = (
                loaded == count and not rebound_overlap and not mapping_errors and not corrupt_titles
                and not callbacks and manual_edit_preserved and export_current
                and result["network_check_responsive"] and result["unprocessed_summary_correct"]
                and result["clear_during_import_stays_empty"]
                and repeat_generation_preserves_manual and undo_restores_manual and provider_routes_isolated
                and not clipped_actions
                and batch_keywords_correct
                and wheel_moves == 90
                and not editor_visibility_events
                and not drag_errors
                and not resize_errors
                and thumbnail_handle_cleared
                and result["decoded_thumbnails"] <= app._thumb_cache_limit
                and hidden_samples == 0
                and abs(app.workspace._get_widget_scaling() - scale) < 0.01
                and all(str(-round(v._apply_widget_scaling(14))) in str(v.title_e.cget("font")) for v in app.card_views)
            )
            result["performance"] = assess_scroll_performance(result)
            (output / "workspace.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            return result
        finally:
            app.destroy()
            module.filedialog.askopenfilenames = original_open
            for name, fn in original_dialogs.items():
                setattr(module.messagebox, name, fn)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=150)
    parser.add_argument("--scale", type=float)
    parser.add_argument("--sample-image", action="append", default=[])
    parser.add_argument("--manifest")
    parser.add_argument("--width", type=int)
    parser.add_argument("--cycles", type=int, default=5)
    args = parser.parse_args()
    os.environ["PIXTAGRA_PACKAGED_TEST_ISOLATION"] = "1"
    os.environ["PSS_NO_STARTUP_DIALOG"] = "1"
    import pixtagra
    sample_images = args.sample_image
    if args.manifest:
        sample_images = read_corpus_manifest(args.manifest)
        args.count = len(sample_images)
    result = run_probe(pixtagra, args.output, args.count, args.scale, sample_images, args.width, args.cycles)
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
