"""Emit reproducible Phase 2 metadata snapshots from temporary synthetic fixtures."""

from __future__ import annotations

import builtins
import json
from pathlib import Path
import sys
import tempfile

import pytest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_stage2_isolation as support


def _result_summary(result):
    return {
        "ok": result["ok"],
        "errors": result["errors"],
        "output_dir": result["output_dir"],
        "saved_paths": result["saved_paths"],
        "save_warnings": result["save_warnings"],
    }


def main():
    with tempfile.TemporaryDirectory(prefix="pixtagra_stage2_metadata_") as root_text:
        root = Path(root_text)
        monkeypatch = pytest.MonkeyPatch()
        generator = support.app_module.__wrapped__(root, monkeypatch)
        module = next(generator)
        try:
            output = root / "output"
            output.mkdir()

            jpeg = root / "source.jpg"
            support._make_jpeg(jpeg, module)
            jpeg_before = support._snapshot(jpeg, module)
            saved = support._run_worker(
                module,
                [{"card": object(), "path": str(jpeg), **support._synth_result()}],
                output_dir=output,
            )
            jpeg_after_source = support._snapshot(jpeg, module)
            jpeg_output = support._snapshot(output / "Same_Name_Title.jpg", module)

            default_target = root / "Same_Name_Title.jpg"
            support._make_jpeg(default_target, module)
            default_before = support._snapshot(default_target, module)
            default_saved = support._run_worker(
                module,
                [{"card": object(), "path": str(default_target), **support._synth_result()}],
                output_dir=None,
            )
            default_after = support._snapshot(default_target, module)
            default_output = support._snapshot(root / "Same_Name_Title_1.jpg", module)

            direct = root / "direct-inject.jpg"
            support._make_jpeg(direct, module)
            direct_before = support._snapshot(direct, module)
            injected = module.inject_metadata(
                str(direct),
                "Replacement Title",
                "Replacement Description",
                ["replacement", "metadata", "test"],
                creator="Replacement Creator",
            )
            direct_output = root / "direct-inject-output.jpg"
            direct_output.write_bytes(injected)
            direct_after = support._snapshot(direct_output, module)

            png = root / "alpha.png"
            png_output_dir = root / "png-output"
            png_output_dir.mkdir()
            support._make_png(png)
            png_before = support._snapshot(png, module)
            png_saved = support._run_worker(
                module,
                [{
                    "card": object(),
                    "path": str(png),
                    "title": "png conversion title",
                    "description": "Synthetic PNG alpha conversion fixture.",
                    "keywords": ["png", "conversion", "test"],
                }],
                output_dir=png_output_dir,
            )
            png_after_source = support._snapshot(png, module)
            png_output = support._snapshot(png_output_dir / "png_conversion_title.png", module)

            print(json.dumps({
                "isolation": {
                    "config_dir_is_temporary": module.APP_CONFIG_DIR.is_relative_to(root),
                    "vault_dir_is_temporary": module.VAULT_DIR.is_relative_to(root),
                    "network_requests": 0,
                },
                "jpeg_save_to_folder": {
                    "result": _result_summary(saved),
                    "source_before": jpeg_before,
                    "source_after": jpeg_after_source,
                    "output": jpeg_output,
                },
                "jpeg_default_save_same_name": {
                    "result": _result_summary(default_saved),
                    "source_before": default_before,
                    "source_after": default_after,
                    "output": default_output,
                },
                "direct_inject_metadata": {
                    "before": direct_before,
                    "after": direct_after,
                },
                "png_save_to_folder": {
                    "result": _result_summary(png_saved),
                    "source_before": png_before,
                    "source_after": png_after_source,
                    "output": png_output,
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
