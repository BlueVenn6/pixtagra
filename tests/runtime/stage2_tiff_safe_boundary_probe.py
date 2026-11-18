"""Isolated P0-03B-3A TIFF safe-boundary probe with no provider calls."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
import sys

import pytest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
import test_p0_03b_tiff_safe_boundary as support


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary(result):
    return {
        "ok": result["ok"],
        "saved_paths": result["saved_paths"],
        "errors": result["errors"],
        "save_warnings": result["save_warnings"],
    }


def main():
    with tempfile.TemporaryDirectory(prefix="pixtagra_p03b3a_probe_") as root_text:
        root = Path(root_text)
        monkeypatch = pytest.MonkeyPatch()
        generator = support.app_module.__wrapped__(root, monkeypatch)
        module = next(generator)
        try:
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            default_source = input_dir / "default.tiff"
            folder_source = input_dir / "folder.tif"
            support._make_tiff(default_source, "multipage")
            support._make_tiff(folder_source, "16bit")
            default_before = _sha256(default_source)
            folder_before = _sha256(folder_source)

            default_result = support._run_worker(module, [support._item(default_source, "Probe Default TIFF")])
            folder_result = support._run_worker(module, [support._item(folder_source, "Probe Folder TIFF")], output_dir=output_dir)

            print(json.dumps({
                "isolation": {
                    "config_dir_is_temporary": module.APP_CONFIG_DIR.is_relative_to(root),
                    "vault_dir_is_temporary": module.VAULT_DIR.is_relative_to(root),
                    "network_requests": 0,
                },
                "default_save": {
                    "result": _summary(default_result),
                    "source_sha256_before": default_before,
                    "source_sha256_after": _sha256(default_source),
                    "new_image_outputs": sorted(
                        path.name for path in input_dir.iterdir() if path not in {default_source, folder_source}
                    ),
                },
                "save_to_folder": {
                    "result": _summary(folder_result),
                    "source_sha256_before": folder_before,
                    "source_sha256_after": _sha256(folder_source),
                    "new_image_outputs": sorted(path.name for path in output_dir.iterdir()),
                },
            }, ensure_ascii=True, sort_keys=True))
        finally:
            try:
                next(generator)
            except StopIteration:
                pass
            monkeypatch.undo()


if __name__ == "__main__":
    main()
