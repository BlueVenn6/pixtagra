import csv
import json
from pathlib import Path
from types import SimpleNamespace

from tests.runtime.test_stage2_isolation import app_module, _bare_app


def metadata():
    return {"title": "Mountain and Lake Landscape beneath Clouds in Evening Light",
            "description": "Mountains rise above a lake with trees along the water and clouds overhead. Evening light falls across the landscape, showing the rocky slopes and the still water in a wide view.",
            "keywords": ["mountain", "lake", "forest", "water", "clouds", "sky", "landscape"], "compliance": {}}


def card(path, data):
    return SimpleNamespace(path=path, _cached_data=data,
        get_data=lambda: (data["title"], data["description"], data["keywords"]),
        get_compliance=lambda: data.get("compliance", {}), sync_from_view=lambda: None)


def test_csv_round_trip_keeps_unicode_comma_filename_and_keyword_order(app_module, tmp_path, monkeypatch):
    app = _bare_app(app_module)
    data = metadata()
    data["keywords"] = app_module.rank_keywords_for_stock(data["keywords"])
    filename = '测试, "photo".jpg'
    app.cards = {filename: card(filename, data)}
    app.data_cache = {}
    destination = tmp_path / "upload.csv"
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **kwargs: str(destination))
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *args: False)
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *args: None)
    app._export_shutterstock_csv()
    with destination.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["Filename"] == filename
    assert rows[0]["Keywords"].split(", ") == data["keywords"]
    assert rows[0]["Description"] == data["description"]
    assert set(rows[0]) == {"Filename", "Description", "Keywords", "Categories", "Editorial", "Mature Content", "Illustration"}


def test_duplicate_basenames_are_blocked_before_csv_destination_is_opened(app_module, monkeypatch):
    app = _bare_app(app_module)
    data = metadata()
    app.cards = {p: card(p, data) for p in ("first/photo.jpg", "second/PHOTO.jpg")}
    app.data_cache = {}
    messages = []
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda *args: messages.append(args))
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **kwargs: (_ for _ in ()).throw(AssertionError("Must not write ambiguous CSV")))
    app._export_shutterstock_csv()
    assert messages[0][0] == "Duplicate filenames"


def test_export_failure_preserves_existing_json_and_current_editor_is_synced(app_module, tmp_path, monkeypatch):
    app = _bare_app(app_module)
    data = metadata()
    app.data_cache = {"photo.jpg": data}
    def sync():
        data["title"] = "Latest title typed in the visible editor"
    app.cards = {"photo.jpg": SimpleNamespace(sync_from_view=sync)}
    destination = tmp_path / "metadata.json"
    destination.write_text("existing export", encoding="utf-8")
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **kwargs: str(destination))
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *args: None)
    messages = []
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda *args: messages.append(args))
    replace = app_module.os.replace
    monkeypatch.setattr(app_module.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("Simulated locked destination")))
    app._export()
    assert destination.read_text() == "existing export"
    assert messages and "locked destination" in messages[0][1]
    monkeypatch.setattr(app_module.os, "replace", replace)
    app._export()
    assert json.loads(destination.read_text(encoding="utf-8"))[0]["title"] == data["title"]


def test_ready_json_excludes_unresolved_content_rights_flags(app_module, monkeypatch):
    app = _bare_app(app_module)
    app.cards = {}
    data = metadata()
    data["compliance"] = {"visible_logo": True}
    app.data_cache = {"photo.jpg": data}
    messages = []
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda *args: messages.append(args))
    app._export_ready()
    assert messages[0][1] == "No ready metadata found"
