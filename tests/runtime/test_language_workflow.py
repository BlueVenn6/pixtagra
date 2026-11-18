"""Real Tk source UI plus local generated fixtures; no user credentials/network."""
import csv
import json
from PIL import Image

from tests.runtime.test_stage2_isolation import app_module, _bare_app
from tests.runtime.test_export_delivery import card
from tests.test_language_support import chinese_metadata


def test_live_ui_language_toggle_persistence_and_canonical_values(app_module, monkeypatch, tmp_path, request):
    from tests.runtime.gui_process import in_fresh_gui_process
    if not in_fresh_gui_process(request):
        return
    monkeypatch.setenv("PIXTAGRA_PACKAGED_TEST_ISOLATION", "1")
    app = app_module.App()
    errors = []
    app.report_callback_exception = lambda *args: errors.append(args)
    try:
        app.update()
        assert app.language_var.get() == "English"
        assert app.test_conn_btn.cget("text") == "Test Connection"
        mode, model = app.mode_var.get(), app.model_var.get()
        app.creator_var.set("Original 作者")
        app._language_menus[0]._dropdown_callback("简体中文")
        app.update()
        assert app.language_var.get() == "简体中文"
        assert app.test_conn_btn.cget("text") == "测试连接"
        assert app.mode_var.get() == mode and app.model_var.get() == model
        assert app.creator_var.get() == "Original 作者"
        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)
        mode_menus = [widget for widget in walk(app) if isinstance(widget, app_module.ctk.CTkOptionMenu)
                      and widget.cget("values") == app_module.PROMPT_MODES]
        assert mode_menus and all(widget._text_label.cget("text") == "📷 通用摄影" for widget in mode_menus)
        assert all(menu._text_label.cget("text") == "简体中文" for menu in app._language_menus)
        for menu in app._language_menus:
            assert menu.winfo_reqwidth() <= menu.master.winfo_width()
        app.is_processing = True
        app._batch_language = "zh"
        app._update_workspace_controls()
        assert all(menu.cget("state") == "disabled" for menu in app._language_menus)
        app.is_processing = False
        app._update_workspace_controls()
        app._on_language_change("English")
        assert app.test_conn_btn.cget("text") == "Test Connection"
        assert app.creator_var.get() == "Original 作者"
        app._on_language_change("简体中文")
        source = tmp_path / "中文测试.jpg"
        Image.new("RGB", (96, 64), "white").save(source)
        monkeypatch.setattr(app_module.filedialog, "askopenfilenames", lambda **kw: [str(source)])
        app.load_images()
        app.update()
        record = app.cards[str(source)]
        assert "20–35" in record.view._field_headings[2].cget("text")
        assert "/35" not in record.view.kw_count_lbl.cget("text")
        record.fill(chinese_metadata())
        app.update()
        assert record.get_data(sync=False)[2] == chinese_metadata()["keywords"]
        assert record.view._field_headings[0].cget("text") == "标题（8–30 个汉字）"
        assert record.view._field_headings[1].cget("text") == "描述（30–120 个汉字）"
        app._on_language_change("English")
        assert record.get_data(sync=False)[2] == chinese_metadata()["keywords"]
        assert record.view._field_headings[0].cget("text") == "Title (8-30 Chinese characters)"
        app._on_language_change("简体中文")
        assert json.loads(app_module.CONFIG_FILE.read_text(encoding="utf-8"))["language"] == "zh"
        assert not errors
    finally:
        app.destroy()
    reopened = app_module.App()
    try:
        reopened.update()
        assert reopened.language_var.get() == "简体中文"
        assert reopened.test_conn_btn.cget("text") == "测试连接"
        assert reopened.cards[str(source)]._cached_data["metadata_language"] == "zh"
    finally:
        reopened.destroy()


def test_chinese_generation_record_json_csv_and_jpeg_round_trip(app_module, tmp_path, monkeypatch):
    app = _bare_app(app_module)
    app._batch_language = "zh"
    app._batch_visual_review_enabled = False
    generated_chinese = chinese_metadata()
    generated_chinese["keywords"] += ["饮品", "饮用器皿", "杯沿", "杯身", "桌面", "深色液体", "散落豆粒", "圆形杯口", "横向构图", "柔和光线"]
    app._call_api = lambda *args, **kwargs: generated_chinese
    app._checkpoint_result = lambda *args: None
    source = tmp_path / "中文图片.jpg"
    Image.new("RGB", (96, 64), "white").save(source)
    from types import SimpleNamespace
    result = app._process(SimpleNamespace(path=str(source)), "Qwen 3.7 Plus", app_module.PROMPT_MODES[0], "SYNTHETIC", "")
    assert result is not None and result["metadata_language"] == "zh"
    assert result["keywords"] == generated_chinese["keywords"]
    app.data_cache = {str(source): result}
    app.cards = {str(source): card(str(source), result)}
    app.cards[str(source)].status = SimpleNamespace(cget=lambda option: "● Ready")
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *args: None)
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *args: False)
    warnings = []
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda *args: warnings.append(args))
    destination = tmp_path / "metadata.json"
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **kw: str(destination))
    app._export()
    assert json.loads(destination.read_text(encoding="utf-8"))[0]["keywords"] == result["keywords"]
    destination = tmp_path / "ready.json"
    app._export_ready()
    assert not destination.exists()  # Visual review was explicitly disabled.
    assert warnings and app_module.keyword_review_warnings(result)
    warnings.clear()
    # Continue the language/file round trip with a completed synthetic audit.
    checked = app_module.apply_keyword_review(result, {
        "visible_facts": ["Synthetic cup fixture", "Synthetic table fixture"], "duplicate_groups": [],
        "checks": [{"id": i, "verdict": "supported", "reason": "Synthetic language fixture"}
                   for i in range(len(result["keywords"]))],
        "caption_checks": [{"field": field, "verdict": "supported", "reason": "Synthetic language fixture"}
                           for field in ("title", "description")],
    }, "Qwen 3.7 Plus", review_metadata=True)
    result.update(checked)
    app._export_ready()
    assert json.loads(destination.read_text(encoding="utf-8"))[0]["title"] == result["title"]
    destination = tmp_path / "metadata.csv"
    app._export_shutterstock_csv()
    assert warnings and not destination.exists()  # Shutterstock officially requires English.
    warnings.clear()
    app._export_for_target("adobe")
    assert not warnings
    with destination.open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["Keywords"].split(", ") == result["keywords"]
    assert row["Title"] == result["title"]
    original = source.read_bytes()
    output = tmp_path / "output.jpg"
    output.write_bytes(app_module.inject_metadata(source, result["title"], result["description"], result["keywords"]))
    app_module.metadata._verify_written_metadata(output, result["title"], result["description"], result["keywords"], source_jpeg_path=source)
    assert source.read_bytes() == original
