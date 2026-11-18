"""Emit isolated PNG and static WebP container-preservation evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile

import pytest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
import test_p0_03b_png_webp_preservation as support


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _result_summary(result):
    return {
        "ok": result["ok"],
        "errors": result["errors"],
        "saved_paths": result["saved_paths"],
        "save_warnings": result["save_warnings"],
    }


def _png_snapshot(path):
    chunks = support._png_chunks(path.read_bytes())
    return {
        "file_sha256": _digest(path.read_bytes()),
        "idat_sha256": _digest(b"".join(payload for kind, payload, _raw in chunks if kind == b"IDAT")),
        "icc_sha256": [_digest(payload) for kind, payload, _raw in chunks if kind == b"iCCP"],
        "unknown_ancillary_sha256": [_digest(raw) for kind, _payload, raw in chunks if kind == b"vpAg"],
        "format": support._image_snapshot(path)["format"],
        "pixel_sha256": support._image_snapshot(path)["pixel"],
        "alpha_sha256": support._image_snapshot(path)["alpha"],
    }


def _webp_snapshot(path):
    chunks = support._webp_chunks(path.read_bytes())
    return {
        "file_sha256": _digest(path.read_bytes()),
        "vp8_vp8l_alph_sha256": [
            {"kind": kind.decode("ascii"), "sha256": _digest(payload)}
            for kind, payload, _raw in chunks
            if kind in {b"VP8 ", b"VP8L", b"ALPH"}
        ],
        "icc_sha256": [_digest(payload) for kind, payload, _raw in chunks if kind == b"ICCP"],
        "unknown_riff_sha256": [_digest(raw) for kind, _payload, raw in chunks if kind == b"UNKN"],
        "format": support._image_snapshot(path)["format"],
        "pixel_sha256": support._image_snapshot(path)["pixel"],
        "alpha_sha256": support._image_snapshot(path)["alpha"],
    }


def main():
    with tempfile.TemporaryDirectory(prefix="pixtagra_p03b2_probe_") as root_text:
        root = Path(root_text)
        monkeypatch = pytest.MonkeyPatch()
        generator = support.app_module.__wrapped__(root, monkeypatch)
        module = next(generator)
        try:
            output_dir = root / "output"
            output_dir.mkdir()

            png = root / "source.png"
            support._make_png(png)
            png_before = _png_snapshot(png)
            png_result = support._run_worker(module, [support._item(png, "Probe PNG")], output_dir=output_dir)
            png_output = output_dir / "Probe_PNG.png"

            webp = root / "source.webp"
            support._make_webp(webp, lossless=False, alpha=True)
            webp_before = _webp_snapshot(webp)
            webp_result = support._run_worker(module, [support._item(webp, "Probe WebP")], output_dir=output_dir)
            webp_output = output_dir / "Probe_WebP.webp"

            print(json.dumps({
                "isolation": {
                    "config_dir_is_temporary": module.APP_CONFIG_DIR.is_relative_to(root),
                    "vault_dir_is_temporary": module.VAULT_DIR.is_relative_to(root),
                    "network_requests": 0,
                },
                "png": {
                    "result": _result_summary(png_result),
                    "source": png_before,
                    "output": _png_snapshot(png_output),
                },
                "webp": {
                    "result": _result_summary(webp_result),
                    "source": webp_before,
                    "output": _webp_snapshot(webp_output),
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
