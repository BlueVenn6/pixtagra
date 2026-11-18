"""P0-03A regression coverage for atomic metadata staging and verification."""

from __future__ import annotations

import hashlib
import builtins
from pathlib import Path

import pytest

from tests.runtime.test_stage2_isolation import (
    _make_jpeg,
    _make_png,
    _make_webp,
    _run_worker,
    app_module,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _item(source: Path, title="Atomic Metadata Title", *, card=None):
    return {
        "card": card if card is not None else object(),
        "path": str(source),
        "title": title,
        "description": "Synthetic P0-03A metadata transaction fixture.",
        "keywords": ["atomic", "metadata", "fixture"],
    }


def _temporary_files(directory: Path):
    return list(directory.glob(".pixtagra_*.tmp"))


def _assert_failed_transaction(result, source, source_hash, output_dir):
    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert result["errors"]
    assert _sha256(source) == source_hash
    assert not list(output_dir.glob("*.jpg"))
    assert _temporary_files(output_dir) == []


def test_metadata_exception_never_publishes_final_output(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)
    target = output_dir / "Atomic_Metadata_Title.jpg"

    def fail_injection(*_args, **_kwargs):
        raise RuntimeError("synthetic metadata injection failure")

    monkeypatch.setattr(app_module.metadata, "inject_metadata", fail_injection)
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert not target.exists()
    assert _sha256(source) == source_hash


def test_metadata_false_result_is_not_reported_as_success(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    target = output_dir / "Atomic_Metadata_Title.jpg"

    monkeypatch.setattr(app_module.metadata, "inject_metadata", lambda *_args, **_kwargs: False)
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert not target.exists()


def test_jpeg_encoding_failure_leaves_no_final_or_temporary_output(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)

    monkeypatch.setattr(app_module.metadata, "_write_jpeg_bytes_to_tempfile", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic JPEG encoding failure")))
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    _assert_failed_transaction(result, source, source_hash, output_dir)
    assert "JPEG encoding failure" in result["errors"][0]


def test_metadata_build_failure_cleans_staged_jpeg(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)

    monkeypatch.setattr(app_module.metadata, "_merge_xmp_payload", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("synthetic metadata build failure")))
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    _assert_failed_transaction(result, source, source_hash, output_dir)
    assert "metadata build failure" in result["errors"][0]


def test_partial_metadata_staging_write_is_cleaned_before_publish(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)
    original_open = builtins.open

    class _PartialWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def write(self, data):
            self.handle.write(data[:32])
            raise OSError("synthetic partial metadata write failure")

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def fail_metadata_write(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if "w" in mode and Path(path).name.startswith(".pixtagra_metadata_"):
            return _PartialWriter(handle)
        return handle

    monkeypatch.setattr(app_module.metadata, "open", fail_metadata_write, raising=False)
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    _assert_failed_transaction(result, source, source_hash, output_dir)
    assert "partial metadata write failure" in result["errors"][0]


def test_post_write_validation_failure_never_publishes_final(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)

    monkeypatch.setattr(app_module.metadata, "_verify_written_metadata", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("synthetic readback verification failure")))
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    _assert_failed_transaction(result, source, source_hash, output_dir)
    assert "readback verification failure" in result["errors"][0]


def test_external_target_race_uses_next_safe_name_without_overwrite(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    external_target = output_dir / "Atomic_Metadata_Title.jpg"
    external_bytes = b"external process output must stay untouched"
    original_publish = app_module._publish_new_output
    calls = []

    def race_publish(tmp_path_value, save_path):
        calls.append(Path(save_path))
        if len(calls) == 1:
            Path(save_path).write_bytes(external_bytes)
            raise FileExistsError("synthetic external publish race")
        return original_publish(tmp_path_value, save_path)

    monkeypatch.setattr(app_module.metadata, "_publish_new_output", race_publish)
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    assert result["ok"] == 1
    assert external_target.read_bytes() == external_bytes
    assert result["saved_paths"] == [str(output_dir / "Atomic_Metadata_Title_1.jpg")]
    assert (output_dir / "Atomic_Metadata_Title_1.jpg").exists()
    assert _temporary_files(output_dir) == []


def test_final_publish_failure_cleans_temporary_files(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)

    monkeypatch.setattr(app_module.metadata, "_publish_new_output", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic final publish failure")))
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)

    _assert_failed_transaction(result, source, source_hash, output_dir)
    assert "final publish failure" in result["errors"][0]


@pytest.mark.parametrize("save_to_folder", [False, True])
def test_successful_transaction_reopens_and_verifies_metadata(app_module, tmp_path, monkeypatch, save_to_folder):
    source = tmp_path / "Atomic_Metadata_Title.jpg" if not save_to_folder else tmp_path / "source.jpg"
    output_dir = tmp_path / "output" if save_to_folder else None
    if output_dir:
        output_dir.mkdir()
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)
    verify_calls = []
    original_verify = app_module._verify_written_metadata

    def track_verify(*args, **kwargs):
        verify_calls.append(Path(args[0]))
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(app_module.metadata, "_verify_written_metadata", track_verify)
    result = _run_worker(app_module, [_item(source)], output_dir=output_dir)
    output = Path(result["saved_paths"][0])

    assert result["ok"] == 1
    assert result["errors"] == []
    assert _sha256(source) == source_hash
    assert output.exists()
    assert output != source
    assert verify_calls and verify_calls[0] != output
    verified = original_verify(output, "Atomic Metadata Title", "Synthetic P0-03A metadata transaction fixture.", ["atomic", "metadata", "fixture"], "Current Creator")
    assert verified["size"] > 0
    assert _temporary_files(output.parent) == []


@pytest.mark.parametrize(
    ("filename", "maker"),
    [
        ("source.jpg", lambda path, module: _make_jpeg(path, module)),
        ("source.png", lambda path, _module: _make_png(path)),
        ("source.webp", lambda path, _module: _make_webp(path)),
    ],
)
def test_metadata_transaction_keeps_all_input_sources_unchanged(app_module, tmp_path, filename, maker):
    source = tmp_path / filename
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    maker(source, app_module)
    source_hash = _sha256(source)

    result = _run_worker(app_module, [_item(source, "Format Metadata Transaction")], output_dir=output_dir)

    assert result["ok"] == 1
    assert _sha256(source) == source_hash
    assert Path(result["saved_paths"][0]).exists()


def test_partial_batch_only_marks_and_clears_successful_card(app_module, tmp_path, monkeypatch):
    source_ok = tmp_path / "ok.jpg"
    source_fail = tmp_path / "fail.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source_ok, app_module)
    _make_jpeg(source_fail, app_module)

    class _Card:
        def __init__(self):
            self.marked_saved = False

        def mark_saved(self):
            self.marked_saved = True

    class _Button:
        def configure(self, **_kwargs):
            return None

    class _DoneHarness:
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
        btns = {"💾 Save": _Button(), "📁 Save→Folder": _Button()}

        def __init__(self):
            self.cleared = []

        def _auto_clear_saved(self, cards):
            self.cleared = list(cards)

    original_inject = app_module.inject_metadata

    def fail_one(path, title, *args, **kwargs):
        if title == "Fail Metadata":
            raise RuntimeError("synthetic batch metadata failure")
        return original_inject(path, title, *args, **kwargs)

    ok_card = _Card()
    fail_card = _Card()
    monkeypatch.setattr(app_module.metadata, "inject_metadata", fail_one)
    result = _run_worker(
        app_module,
        [_item(source_ok, "OK Metadata", card=ok_card), _item(source_fail, "Fail Metadata", card=fail_card)],
        output_dir=output_dir,
    )
    messages = []
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda title, message: messages.append((title, message)))
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *_args, **_kwargs: False)
    done = _DoneHarness()
    app_module.App._save_done(done, result["ok"], result["saved_cards"], output_dir, result["errors"], result["saved_paths"], result["save_warnings"])

    assert result["ok"] == 1
    assert result["saved_cards"] == [ok_card]
    assert fail_card.marked_saved is False
    assert ok_card.marked_saved is True
    assert done.cleared == [ok_card]
    assert (output_dir / "OK_Metadata.jpg").exists()
    assert not (output_dir / "Fail_Metadata.jpg").exists()
    assert any("batch metadata failure" in message for _, message in messages)
