from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageCms, TiffImagePlugin
import piexif
import pytest

from tests.runtime import test_p0_03b_png_webp_preservation as container_support
from tests.runtime.test_stage2_isolation import _WorkerHarness, _make_jpeg, app_module


TIFF_BLOCKED_TEXT = (
    "\u5f53\u524d\u7248\u672c\u6682\u4e0d\u652f\u6301\u5b89\u5168\u4fdd\u5b58 TIFF \u56fe\u7247\u3002"
    "\u539f\u59cb TIFF \u672a\u88ab\u4fee\u6539\uff1b\u4f60\u4ecd\u53ef\u5bfc\u51fa JSON \u6216 CSV\u3002"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _icc_profile() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _item(source: Path, title="TIFF Safety Output", *, card=None):
    return {
        "card": card if card is not None else object(),
        "path": str(source),
        "title": title,
        "description": "Synthetic TIFF fixture used only for isolated safety testing.",
        "keywords": ["tiff", "safety", "fixture"],
    }


def _run_worker(module, items, output_dir=None):
    harness = _WorkerHarness()
    module.App._save_worker(
        harness,
        items,
        output_dir=output_dir,
        creator="Current Creator",
        country="Testland",
        date_created="2026-08-02",
    )
    assert harness.done is not None
    return harness.done


def _save_tiff(image: Image.Image, path: Path, **kwargs):
    try:
        image.save(path, "TIFF", **kwargs)
    finally:
        image.close()


def _make_tiff(path: Path, kind: str):
    if kind == "gray":
        _save_tiff(Image.new("L", (12, 10), 80), path)
    elif kind == "rgba":
        _save_tiff(Image.new("RGBA", (12, 10), (40, 50, 60, 120)), path)
    elif kind == "palette":
        image = Image.new("P", (12, 10), 1)
        image.putpalette([0, 0, 0, 255, 128, 32] + [0, 0, 0] * 254)
        _save_tiff(image, path)
    elif kind == "icc":
        _save_tiff(Image.new("RGB", (12, 10), (70, 80, 90)), path, icc_profile=_icc_profile())
    elif kind == "exif":
        exif = {"0th": {piexif.ImageIFD.Artist: b"Legacy Creator"}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        _save_tiff(Image.new("RGB", (12, 10), (70, 80, 90)), path, exif=piexif.dump(exif))
    elif kind == "xmp":
        info = TiffImagePlugin.ImageFileDirectory_v2()
        info[700] = b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>SyntheticTiffXmp</x:xmpmeta>"
        _save_tiff(Image.new("RGB", (12, 10), (70, 80, 90)), path, tiffinfo=info)
    elif kind == "multipage":
        first = Image.new("RGB", (12, 10), (70, 80, 90))
        second = Image.new("RGB", (12, 10), (20, 30, 40))
        try:
            first.save(path, "TIFF", save_all=True, append_images=[second], compression="tiff_lzw")
        finally:
            first.close()
            second.close()
    elif kind == "16bit":
        _save_tiff(Image.frombytes("I;16", (12, 10), b"\x00\x10" * 120), path)
    elif kind == "compressed":
        _save_tiff(Image.new("RGB", (12, 10), (70, 80, 90)), path, compression="tiff_lzw")
    else:
        _save_tiff(Image.new("RGB", (12, 10), (70, 80, 90)), path)


TIFF_FIXTURES = [
    ("single.tif", "rgb"),
    ("single.tiff", "rgb"),
    ("gray.tiff", "gray"),
    ("alpha.tiff", "rgba"),
    ("palette.tiff", "palette"),
    ("icc.tiff", "icc"),
    ("exif.tiff", "exif"),
    ("xmp.tiff", "xmp"),
    ("multipage.tiff", "multipage"),
    ("sixteen_bit.tiff", "16bit"),
    ("compressed.tiff", "compressed"),
]


@pytest.mark.parametrize("filename,kind", TIFF_FIXTURES)
@pytest.mark.parametrize("save_to_folder", [False, True])
def test_tiff_save_is_blocked_without_image_output(app_module, tmp_path, filename, kind, save_to_folder):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    source = input_dir / filename
    _make_tiff(source, kind)
    source_hash = _sha256(source)
    output_dir = tmp_path / "output" if save_to_folder else None
    if output_dir:
        output_dir.mkdir()

    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    target_dir = output_dir or input_dir
    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert len(result["errors"]) == 1
    assert source.name in result["errors"][0]
    assert "TIFF" in result["errors"][0]
    assert TIFF_BLOCKED_TEXT in result["errors"][0]
    assert _sha256(source) == source_hash
    assert list(target_dir.glob("TIFF_Safety_Output.*")) == []
    assert not list(target_dir.glob(".pixtagra_*.tmp"))


def test_tiff_does_not_touch_existing_target_or_reserve_a_path(app_module, tmp_path, monkeypatch):
    source = tmp_path / "input" / "source.tiff"
    source.parent.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_tiff(source, "rgb")
    source_hash = _sha256(source)
    existing_target = output_dir / "TIFF_Safety_Output.jpg"
    existing_target.write_bytes(b"existing output must remain unchanged")
    existing_hash = _sha256(existing_target)
    called = []

    def unexpected_reservation(*_args, **_kwargs):
        called.append("reserve")
        raise AssertionError("TIFF must be blocked before path reservation")

    monkeypatch.setattr(app_module.metadata, "_reserve_safe_output_path", unexpected_reservation)
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    assert called == []
    assert result["ok"] == 0
    assert result["saved_paths"] == []
    assert TIFF_BLOCKED_TEXT in result["errors"][0]
    assert _sha256(source) == source_hash
    assert _sha256(existing_target) == existing_hash
    assert list(output_dir.iterdir()) == [existing_target]


def test_tiff_does_not_enter_encoding_or_metadata_paths(app_module, tmp_path, monkeypatch):
    source = tmp_path / "input" / "source.tiff"
    source.parent.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_tiff(source, "rgba")
    calls = []

    def unexpected(name):
        def fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"TIFF must not call {name}")

        return fail

    monkeypatch.setattr(app_module.metadata, "_write_jpeg_to_tempfile", unexpected("jpeg_encode"))
    monkeypatch.setattr(app_module.metadata, "inject_metadata", unexpected("metadata"))
    monkeypatch.setattr(app_module.metadata, "_publish_new_output", unexpected("publish"))
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    assert calls == []
    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert TIFF_BLOCKED_TEXT in result["errors"][0]
    assert list(output_dir.iterdir()) == []


def test_tiff_failure_card_is_not_marked_or_auto_cleared(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.tiff"
    _make_tiff(source, "rgb")

    class Card:
        marked_saved = False

        def mark_saved(self):
            self.marked_saved = True

    class Button:
        def configure(self, **_kwargs):
            return None

    class DoneHarness:
        def _update_stats(self):
            self.stats_refreshed = True

        class _Progress:
            def set(self, value):
                self.value = value
            def configure(self, **values):
                self.values = values
        prog = _Progress()
        prog_lbl = _Progress()
        is_saving = True
        btns = {"\U0001f4be Save": Button(), "\U0001f4c1 Save\u2192Folder": Button()}

        def __init__(self):
            self.cleared = []

        def _auto_clear_saved(self, cards):
            self.cleared = list(cards)

    card = Card()
    result = _run_worker(app_module, [_item(source, card=card)])
    warnings = []
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda title, message: warnings.append((title, message)))
    done = DoneHarness()
    app_module.App._save_done(done, result["ok"], result["saved_cards"], None, result["errors"], result["saved_paths"], result["save_warnings"])

    assert result["ok"] == 0
    assert card.marked_saved is False
    assert done.cleared == []
    assert any(TIFF_BLOCKED_TEXT in message for _title, message in warnings)


def test_mixed_batch_keeps_tiff_card_and_saves_supported_formats(app_module, tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    jpeg = input_dir / "photo.jpg"
    png = input_dir / "graphic.png"
    webp = input_dir / "graphic.webp"
    tiff = input_dir / "archive.tiff"
    _make_jpeg(jpeg, app_module)
    container_support._make_png(png)
    container_support._make_webp(webp)
    _make_tiff(tiff, "multipage")
    tiff_hash = _sha256(tiff)
    jpeg_card, png_card, webp_card, tiff_card = object(), object(), object(), object()

    result = _run_worker(
        app_module,
        [
            _item(jpeg, "Mixed JPEG", card=jpeg_card),
            _item(png, "Mixed PNG", card=png_card),
            _item(webp, "Mixed WebP", card=webp_card),
            _item(tiff, "Mixed TIFF", card=tiff_card),
        ],
        output_dir=output_dir,
    )

    assert result["ok"] == 3
    assert result["saved_cards"] == [jpeg_card, png_card, webp_card]
    assert tiff_card not in result["saved_cards"]
    assert {Path(path).suffix.lower() for path in result["saved_paths"]} == {".jpg", ".png", ".webp"}
    assert not (output_dir / "Mixed_TIFF.jpg").exists()
    assert _sha256(tiff) == tiff_hash
    assert any(TIFF_BLOCKED_TEXT in error for error in result["errors"])


def test_tiff_analysis_results_remain_exportable_as_json_and_csv(app_module, tmp_path, monkeypatch):
    source = tmp_path / "archive.tiff"
    _make_tiff(source, "rgb")
    result = {
        "title": "Woman Working on Laptop at Bright Modern Home Office",
        "description": (
            "A woman works on a laptop at a wooden desk in a bright modern home office. "
            "Natural window light reveals a notebook, coffee cup, and houseplant while she "
            "concentrates on remote professional tasks."
        ),
        "keywords": [
            "woman", "laptop", "home office", "remote work", "wooden desk", "daylight",
            "window light", "indoor", "seated", "typing", "computer", "workspace",
            "professional woman", "freelancer", "technology device", "modern interior",
            "casual clothing", "side view", "notebook", "coffee cup", "houseplant",
            "concentration", "adult", "one person", "natural lighting", "online work",
            "digital device", "table surface", "living room", "productivity", "communication",
            "domestic life", "working indoors", "screen", "chair",
        ],
    }
    result["keywords"] = app_module.rank_keywords_for_stock(result["keywords"])
    json_path = tmp_path / "metadata.json"
    csv_path = tmp_path / "shutterstock.csv"
    messages = []

    class JsonHarness:
        _workspace_busy = app_module.App._workspace_busy
        cards = {}
        data_cache = {str(source): result}

    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **_kwargs: str(json_path))
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *args: messages.append(args))
    app_module.App._export(JsonHarness())
    exported_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert exported_json == [{"file": source.name, **result}]

    class Card:
        def get_data(self):
            return result["title"], result["description"], result["keywords"]

    class CsvHarness:
        _workspace_busy = app_module.App._workspace_busy
        cards = {str(source): Card()}
        data_cache = {}

    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda **_kwargs: str(csv_path))
    monkeypatch.setattr(
        app_module.messagebox,
        "askyesno",
        lambda title, *_args, **_kwargs: title == "Compliance review required",
    )
    app_module.App._export_shutterstock_csv(CsvHarness())
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["Filename"] == source.name
    assert rows[0]["Description"] == result["description"]
    assert rows[0]["Keywords"] == ", ".join(result["keywords"])
    assert rows[0]["Categories"] == "Business/Finance, Buildings/Landmarks"


def test_shutterstock_csv_category_fallback_does_not_invent_a_second_category(app_module):
    row = app_module._build_shutterstock_csv_row(
        "object.jpg",
        "Minimal isolated object",
        "A minimal isolated object on a plain surface.",
        ["object", "minimal", "isolated"],
    )

    assert row["Categories"] == "Objects"


def test_shutterstock_csv_blocks_records_that_fail_the_quality_gate(app_module, monkeypatch):
    warnings = []
    save_dialog_calls = []

    class Card:
        def get_data(self):
            return "Short title", "One short sentence.", ["object"]

    class CsvHarness:
        _workspace_busy = app_module.App._workspace_busy
        cards = {"invalid.jpg": Card()}
        data_cache = {}

    monkeypatch.setattr(
        app_module.filedialog,
        "asksaveasfilename",
        lambda **_kwargs: save_dialog_calls.append(True),
    )
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda *args: warnings.append(args))

    app_module.App._export_shutterstock_csv(CsvHarness())

    assert save_dialog_calls == []
    assert warnings
    assert warnings[0][0] == "Metadata quality check failed"
