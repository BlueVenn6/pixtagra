"""P0-02 regression coverage for output paths that must never replace inputs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading

import pytest

from tests.runtime.test_stage2_isolation import (
    _make_jpeg,
    _make_png,
    _make_webp,
    _run_worker,
    _synth_result,
    app_module,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _item(source: Path, title: str, *, card=None):
    return {
        "card": card if card is not None else object(),
        "path": str(source),
        "title": title,
        "description": "Synthetic P0-02 output-path safety fixture.",
        "keywords": ["safe", "output", "fixture"],
    }


def _saved_paths(result):
    return [Path(path) for path in result["saved_paths"]]


def test_default_save_same_generated_name_never_replaces_source(app_module, tmp_path):
    source = tmp_path / "Same_Name_Title.jpg"
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)

    result = _run_worker(
        app_module,
        [{"card": object(), "path": str(source), **_synth_result()}],
        output_dir=None,
    )

    expected_output = tmp_path / "Same_Name_Title_1.jpg"
    assert result["ok"] == 1
    assert _sha256(source) == source_hash
    assert expected_output.exists()
    assert _saved_paths(result) == [expected_output]


def test_case_variant_of_input_name_is_still_a_conflict(app_module, tmp_path):
    source = tmp_path / "Case_Title.jpg"
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)

    result = _run_worker(app_module, [_item(source, "case title")], output_dir=None)
    outputs = _saved_paths(result)

    assert result["ok"] == 1
    assert _sha256(source) == source_hash
    assert outputs == [tmp_path / "case_title_1.jpg"]
    assert app_module._normalized_output_path(outputs[0]) != app_module._normalized_output_path(source)


def test_existing_outputs_increment_without_overwriting(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)
    title = "Collision Name"
    existing = [tmp_path / "Collision_Name.jpg", tmp_path / "Collision_Name_1.jpg", tmp_path / "Collision_Name_2.jpg"]
    expected_hashes = {}
    for index, target in enumerate(existing):
        target.write_bytes(f"existing-output-{index}".encode("ascii"))
        expected_hashes[target] = _sha256(target)

    result = _run_worker(app_module, [_item(source, title)], output_dir=None)

    assert result["ok"] == 1
    assert _sha256(source) == source_hash
    assert _saved_paths(result) == [tmp_path / "Collision_Name_3.jpg"]
    assert (tmp_path / "Collision_Name_3.jpg").exists()
    assert {_sha256(path) for path in existing} == set(expected_hashes.values())
    for path, expected_hash in expected_hashes.items():
        assert _sha256(path) == expected_hash


def test_batch_output_cannot_replace_another_input(app_module, tmp_path):
    first = tmp_path / "first.jpg"
    protected_second = tmp_path / "Protected_Target.jpg"
    _make_jpeg(first, app_module)
    _make_jpeg(protected_second, app_module)
    first_hash = _sha256(first)
    second_hash = _sha256(protected_second)

    result = _run_worker(
        app_module,
        [_item(first, "Protected Target"), _item(protected_second, "Second Result")],
        output_dir=None,
    )
    outputs = _saved_paths(result)

    assert result["ok"] == 2
    assert _sha256(first) == first_hash
    assert _sha256(protected_second) == second_hash
    assert outputs == [tmp_path / "Protected_Target_1.jpg", tmp_path / "Second_Result.jpg"]
    assert all(
        app_module._normalized_output_path(output)
        not in {app_module._normalized_output_path(first), app_module._normalized_output_path(protected_second)}
        for output in outputs
    )


def test_same_cleaned_title_gets_distinct_outputs_in_one_batch(app_module, tmp_path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    _make_jpeg(first, app_module)
    _make_jpeg(second, app_module)
    first_hash = _sha256(first)
    second_hash = _sha256(second)

    result = _run_worker(
        app_module,
        [_item(first, "Twin/Name"), _item(second, "Twin:Name")],
        output_dir=None,
    )

    assert result["ok"] == 2
    assert _sha256(first) == first_hash
    assert _sha256(second) == second_hash
    assert _saved_paths(result) == [tmp_path / "Twin_Name.jpg", tmp_path / "Twin_Name_1.jpg"]


@pytest.mark.parametrize(
    ("filename", "maker", "suffix"),
    [
        ("input.png", lambda path, _module: _make_png(path), ".png"),
        ("input.webp", lambda path, _module: _make_webp(path), ".webp"),
    ],
)
def test_non_jpeg_default_save_keeps_source_unchanged(app_module, tmp_path, filename, maker, suffix):
    source = tmp_path / filename
    maker(source, app_module)
    source_hash = _sha256(source)

    result = _run_worker(app_module, [_item(source, "Format Safe Output")], output_dir=None)
    outputs = _saved_paths(result)

    assert result["ok"] == 1
    assert _sha256(source) == source_hash
    assert outputs == [tmp_path / f"Format_Safe_Output{suffix}"]
    assert app_module._normalized_output_path(outputs[0]) != app_module._normalized_output_path(source)


def test_explicit_save_to_folder_never_replaces_source_in_same_directory(app_module, tmp_path):
    source = tmp_path / "Same_Name_Title.jpg"
    _make_jpeg(source, app_module)
    source_hash = _sha256(source)

    result = _run_worker(app_module, [_item(source, "Same Name Title")], output_dir=tmp_path)

    assert result["ok"] == 1
    assert _sha256(source) == source_hash
    assert _saved_paths(result) == [tmp_path / "Same_Name_Title_1.jpg"]


def test_invalid_output_directory_fails_without_marking_card_saved(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    invalid_output = tmp_path / "not-a-directory"
    _make_jpeg(source, app_module)
    invalid_output.write_text("synthetic file", encoding="utf-8")
    source_hash = _sha256(source)
    card = object()

    result = _run_worker(app_module, [_item(source, "Failure Target", card=card)], output_dir=invalid_output)

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["saved_paths"] == []
    assert "Cannot create a safe output path" in result["errors"][0]
    assert _sha256(source) == source_hash


def test_failed_save_done_keeps_cards_and_shows_failure_reason(app_module, tmp_path, monkeypatch):
    messages = []

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
            self.cleared = None

        def _auto_clear_saved(self, cards):
            self.cleared = list(cards)

    harness = _DoneHarness()
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda title, message: messages.append((title, message)))
    app_module.App._save_done(
        harness,
        0,
        [],
        tmp_path / "not-a-directory",
        ["source.jpg: Cannot create a safe output path: output directory is unavailable"],
        [],
    )

    assert harness.cleared is None
    assert any("Cannot create a safe output path" in message for _, message in messages)


def test_success_message_uses_actual_saved_path(app_module, tmp_path, monkeypatch):
    messages = []

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

        def _auto_clear_saved(self, _cards):
            return None

    saved_path = str(tmp_path / "actual-output.jpg")
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda title, message: messages.append((title, message)))
    app_module.App._save_done(_DoneHarness(), 1, [object()], None, [], [saved_path])

    assert messages == [("Result", f"Saved 1 images:\n{saved_path}")]


def test_path_normalization_handles_absolute_relative_slashes_and_case(app_module, tmp_path, monkeypatch):
    source = tmp_path / "Nested" / "Case_Title.jpg"
    source.parent.mkdir()
    source.write_bytes(b"synthetic path identity fixture")
    monkeypatch.chdir(tmp_path)
    relative = Path("Nested") / "." / "child" / ".." / "Case_Title.jpg"
    forward_slashes = str(source).replace("\\", "/")
    case_variant = str(source).swapcase()

    expected = app_module._normalized_output_path(source)
    assert app_module._normalized_output_path(relative) == expected
    assert app_module._normalized_output_path(forward_slashes) == expected
    assert app_module._normalized_output_path(case_variant) == expected


def test_concurrent_reservations_are_distinct_and_released(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source.write_bytes(b"synthetic source")
    results = []
    reservations = []
    errors = []
    gate = threading.Barrier(2)

    def reserve():
        local = set()
        try:
            gate.wait()
            results.append(app_module._reserve_safe_output_path(output_dir, "Shared.jpg", [source], local))
            reservations.append(local)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reserve), threading.Thread(target=reserve)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert errors == []
        assert sorted(path.name for path in results) == ["Shared.jpg", "Shared_1.jpg"]
    finally:
        for local in reservations:
            app_module._release_safe_output_paths(local)

    assert app_module._RESERVED_OUTPUT_PATHS == set()
