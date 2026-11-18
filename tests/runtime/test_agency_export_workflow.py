import copy
import csv
from pathlib import Path
from types import SimpleNamespace
import time

from PIL import Image
import pytest

from tests.runtime.test_stage2_isolation import app_module, _bare_app
from tests.runtime.test_export_delivery import card, metadata


@pytest.mark.parametrize("target,headers", [
    ("adobe", ["Filename", "Title", "Keywords", "Category", "Releases"]),
    ("getty", ["file name", "title", "description", "keywords"]),
    ("shutterstock", ["Filename", "Description", "Keywords", "Categories", "Illustration", "Mature Content", "Editorial"]),
    ("vcg", ["文件名", "标题", "关键词（手动录入参考）", "核心关键词建议（后台确认）", "描述（参考）"]),
])
def test_app_selected_target_writes_the_corresponding_format(app_module, tmp_path, monkeypatch, target, headers):
    app = _bare_app(app_module)
    record = metadata()
    app.cards = {"photo.jpg": card("photo.jpg", record)}
    app.data_cache = {"photo.jpg": record}
    app._export_target_var = SimpleNamespace(get=lambda: target)
    destination = tmp_path / "metadata.csv"
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **kw: str(destination))
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *args: None)
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *args: True)
    app._export_for_target()
    with destination.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == headers
        row = next(reader)
    assert (row.get("Keywords") or row.get("keywords") or row.get("关键词（手动录入参考）")).split(", ") == record["keywords"]


def test_adobe_rejection_does_not_open_destination_or_truncate_metadata(app_module, monkeypatch):
    app = _bare_app(app_module)
    record = metadata()
    record["title"] = "landscape " * 9
    app.cards = {"photo.jpg": card("photo.jpg", record)}
    app.data_cache = {}
    before = copy.deepcopy(record)
    warnings = []
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda *args: warnings.append(args))
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **kw: pytest.fail("Invalid CSV must not ask for a file"))
    app._export_for_target("adobe")
    assert warnings and "70" in warnings[0][1]
    assert record == before


@pytest.mark.parametrize("target", ["adobe", "getty", "shutterstock"])
def test_export_write_error_retains_existing_file(app_module, tmp_path, monkeypatch, target):
    app = _bare_app(app_module)
    record = metadata()
    app.cards = {"photo.jpg": card("photo.jpg", record)}
    app.data_cache = {}
    destination = tmp_path / "keep.csv"
    destination.write_bytes(b"original")
    errors = []
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **kw: str(destination))
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *args: True)
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda *args: errors.append(args))
    monkeypatch.setattr(app_module.os, "replace", lambda *args: (_ for _ in ()).throw(PermissionError("locked")))
    app._export_for_target(target)
    assert errors and destination.read_bytes() == b"original"


@pytest.mark.parametrize("scale", [1.0, 2.25])
def test_real_dropdown_switch_language_busy_state_and_alamy_worker(app_module, monkeypatch, tmp_path, request, scale):
    from tests.runtime.gui_process import in_fresh_gui_process
    if not in_fresh_gui_process(request):
        return
    monkeypatch.setenv("PIXTAGRA_PACKAGED_TEST_ISOLATION", "1")
    app = app_module.App()
    callback_errors, messages = [], []
    app.report_callback_exception = lambda *args: callback_errors.append(args)
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *args: messages.append(args))
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda *args: pytest.fail(str(args)))
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *args: True)
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (96, 64), "white").save(source)
    original = source.read_bytes()
    try:
        app_module.ctk.set_widget_scaling(scale)
        app.update()
        monkeypatch.setattr(app_module.filedialog, "askopenfilenames", lambda **kw: [str(source)])
        app.load_images()
        app.update()
        record = app.cards[str(source)]
        record.fill(metadata())
        app._toggle_workspace_options("output")
        app.update()
        assert app._export_target_menu.winfo_ismapped()
        assert app._export_hint_label.winfo_ismapped()
        assert app._export_target_menu.cget("values") == [t.label for t in app_module.EXPORT_TARGETS]
        assert app._export_target_menu.cget("dropdown_font")[1] == 12
        for target in app_module.EXPORT_TARGETS:
            app._export_target_menu._dropdown_callback(target.label)
            app.update()
            assert app._selected_export_target().key == target.key
            expected = "disabled" if target.kind == "unverified" else "normal"
            assert app.btns["📊 SS CSV"].cget("state") == expected
            assert 0 <= app._export_target_menu.winfo_x()
            assert app._export_target_menu.winfo_x() + app._export_target_menu.winfo_width() <= app._export_target_menu.master.winfo_width()
            if target.kind == "csv":
                csv_file = tmp_path / (target.key + ".csv")
                monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **kw: str(csv_file))
                app.btns["📊 SS CSV"].invoke()
                with csv_file.open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    assert reader.fieldnames == list(target.headers)
                    assert len(list(reader)) == 1
        record._cached_data["title"] = "landscape " * 9
        app._export_target_menu._dropdown_callback("Adobe Stock · CSV")
        assert app.btns["📊 SS CSV"].cget("state") == "disabled"
        assert "70" in app._export_hint_label.cget("text")
        record.fill(metadata())
        app._on_language_change("简体中文")
        app._export_target_menu._dropdown_callback("视觉中国 · 手动录入参考表")
        assert app._selected_export_target().key == "vcg"
        assert "不能直接上传" in app._export_hint_label.cget("text")
        app._on_language_change("English")
        assert "not an upload template" in app._export_hint_label.cget("text")
        app._on_language_change("简体中文")
        app._export_target_menu._dropdown_callback("Alamy · IPTC 图片")
        assert app._selected_export_target().key == "alamy"
        assert record.get_data(sync=False)[0] == metadata()["title"]
        app.is_processing = True
        app._update_workspace_controls()
        app._update_save_export_buttons()
        assert app._export_target_menu.cget("state") == "disabled"
        assert app.btns["📊 SS CSV"].cget("state") == "disabled"
        app.is_processing = False
        app._update_workspace_controls()
        app._update_save_export_buttons()
        # Exercise the actual selected export command and worker/main-thread queue.
        monkeypatch.setattr(app_module.filedialog, "askdirectory", lambda **kw: str(tmp_path))
        app.btns["📊 SS CSV"].invoke()
        assert app.is_saving
        assert app._export_target_menu.cget("state") == "disabled"
        deadline = time.monotonic() + 15
        def wait_for_export():
            if not app.is_saving or time.monotonic() >= deadline:
                app.quit()
            else:
                app.after(10, wait_for_export)
        app.after(10, wait_for_export)
        app.mainloop()  # Use the actual Tk event loop while the worker runs.
        assert not app.is_saving and messages
        outputs = list(tmp_path.glob("PixTagra_Alamy_*/photo.jpg"))
        assert len(outputs) == 1
        app_module.metadata._verify_written_metadata(outputs[0], metadata()["title"], metadata()["title"], metadata()["keywords"], source_jpeg_path=source)
        assert str(source) in app.cards and source.read_bytes() == original
        assert record.get_data(sync=False)[2] == metadata()["keywords"]
        app._toggle_workspace_options("output")
        assert not app._export_target_menu.winfo_ismapped()
        assert not app._export_hint_label.winfo_ismapped()
        assert not callback_errors
    finally:
        app_module.ctk.set_widget_scaling(1.0)
        app.destroy()
