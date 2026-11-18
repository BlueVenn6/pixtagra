"""Phase 2 isolated runtime and safety tests.

Every source-module import uses temporary PSS config, vault home, and temp paths.
No test calls App.analyze, requests a real provider, or uses a real user key/image.
"""

from __future__ import annotations

import base64
import builtins
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from PIL import Image, ImageCms, PngImagePlugin
import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
APP_PATH = SOURCE_ROOT / "pixtagra.py"
TEST_KEY = "TEST_KEY_DO_NOT_USE"


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _WorkerHarness:
    def __init__(self):
        self.done = None

    def after(self, _delay, callback):
        callback()

    def _save_done(self, ok, saved_cards, output_dir, errors, saved_paths=None, save_warnings=None):
        self.done = {
            "ok": ok,
            "saved_cards": list(saved_cards),
            "output_dir": str(output_dir) if output_dir else None,
            "errors": list(errors),
            "saved_paths": list(saved_paths or []),
            "save_warnings": list(save_warnings or []),
        }


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    """Import a fresh production module bound only to a test home/config."""
    test_home = tmp_path / "home"
    test_config = tmp_path / "config"
    test_temp = tmp_path / "temp"
    for path in (test_home, test_config, test_temp):
        path.mkdir()

    monkeypatch.setenv("PSS_CONFIG_DIR", str(test_config))
    monkeypatch.setenv("PSS_CONFIG_FILE", str(test_config / "config.json"))
    monkeypatch.setenv("HOME", str(test_home))
    monkeypatch.setenv("USERPROFILE", str(test_home))
    monkeypatch.setenv("HOMEDRIVE", test_home.drive)
    monkeypatch.setenv("HOMEPATH", test_home.root)
    monkeypatch.setenv("TEMP", str(test_temp))
    monkeypatch.setenv("TMP", str(test_temp))
    monkeypatch.setenv("PSS_NO_STARTUP_DIALOG", "1")

    # The existing provider tests may install lightweight import stubs. Metadata
    # fixtures need the actual piexif package, never its test stub.
    existing = sys.modules.get("piexif")
    if existing is not None and not hasattr(existing, "dump"):
        sys.modules.pop("piexif", None)
        sys.modules.pop("piexif.helper", None)
    importlib.import_module("piexif")
    importlib.import_module("piexif.helper")

    module_name = f"pixtagra_stage2_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.APP_CONFIG_DIR.resolve() == test_config.resolve()
    assert module.CONFIG_FILE.resolve() == (test_config / "config.json").resolve()
    assert module.VAULT_DIR.resolve() == (test_home / ".pictureseo").resolve()
    yield module
    sys.modules.pop(module_name, None)


def _bare_app(module, key=TEST_KEY):
    app = object.__new__(module.App)
    app._config_data = {}
    app.key_var = _Var(key)
    app.mode_var = _Var("📷 General Photography")
    return app


def _synth_result():
    return {
        "title": "Same Name Title",
        "description": "A synthetic image used only for isolated PixTagra runtime safety testing.",
        "keywords": ["blue", "rectangle", "test", "synthetic"],
    }


def _icc_profile_bytes():
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _jpeg_segment(marker, payload):
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _add_legacy_xmp_and_iptc(path):
    raw = path.read_bytes()
    xmp = (
        b"http://ns.adobe.com/xap/1.0/\x00"
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b"<rdf:Description>LegacyXmpMarker Legacy Title Legacy Description "
        b"Legacy Keywords</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    def iptc_record(record, dataset, value):
        return b"\x1c" + bytes([record, dataset]) + len(value).to_bytes(2, "big") + value

    iptc = b"".join(
        (
            iptc_record(1, 90, b"\x1b%G"),
            iptc_record(2, 5, b"Legacy Title"),
            iptc_record(2, 25, b"Legacy Keyword"),
            iptc_record(2, 80, b"Legacy Creator"),
            iptc_record(2, 116, b"Legacy Copyright"),
            iptc_record(2, 200, b"LegacyIptcMarker"),
        )
    )
    padding = b"\x00" if len(iptc) % 2 else b""
    resource = b"8BIM\x04\x04\x00\x00" + len(iptc).to_bytes(4, "big") + iptc + padding
    app13 = b"Photoshop 3.0\x00" + resource
    path.write_bytes(raw[:2] + _jpeg_segment(0xE1, xmp) + _jpeg_segment(0xED, app13) + raw[2:])


def _make_jpeg(path, module):
    image = Image.new("RGB", (16, 12))
    image.putdata([(x * 10, y * 15, (x + y) * 7) for y in range(12) for x in range(16)])
    exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif["0th"][module.piexif.ImageIFD.Artist] = b"Legacy Creator"
    exif["0th"][module.piexif.ImageIFD.Copyright] = b"Legacy Copyright"
    image.save(path, "JPEG", quality=100, subsampling=0, exif=module.piexif.dump(exif), icc_profile=_icc_profile_bytes())
    image.close()
    _add_legacy_xmp_and_iptc(path)


def _make_png(path):
    image = Image.new("RGBA", (12, 10), (10, 20, 30, 100))
    info = PngImagePlugin.PngInfo()
    info.add_text("LegacyDescription", "Synthetic alpha image")
    image.save(path, "PNG", pnginfo=info, icc_profile=_icc_profile_bytes())
    image.close()


def _make_webp(path):
    image = Image.new("RGBA", (12, 10), (40, 50, 60, 120))
    image.save(path, "WEBP", lossless=True, icc_profile=_icc_profile_bytes())
    image.close()


def _make_tiff(path):
    image = Image.new("RGB", (12, 10), (70, 80, 90))
    image.save(path, "TIFF", icc_profile=_icc_profile_bytes())
    image.close()


def _decode_exif(value):
    if isinstance(value, bytes):
        return value.rstrip(b"\x00").decode("utf-8", errors="replace")
    return str(value or "")


def _snapshot(path, module):
    raw = path.read_bytes()
    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        pixel_hash = hashlib.sha256(rgba.tobytes()).hexdigest()
        bands = image.getbands()
        snapshot = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "format": image.format,
            "size": list(image.size),
            "pixel_sha256": pixel_hash,
            "has_alpha": "A" in bands,
            "icc_present": bool(image.info.get("icc_profile")),
        }
        rgba.close()
    snapshot.update(
        {
            "has_xmp": b"http://ns.adobe.com/xap/1.0/" in raw,
            "has_iptc": b"Photoshop 3.0\x00" in raw,
            "legacy_xmp_marker": b"LegacyXmpMarker" in raw,
            "legacy_iptc_marker": b"LegacyIptcMarker" in raw,
            "has_new_xmptk": b'x:xmptk="PixTagra"' in raw,
        }
    )
    try:
        exif = module.piexif.load(str(path))
        snapshot["creator"] = _decode_exif(exif.get("0th", {}).get(module.piexif.ImageIFD.Artist, b""))
        snapshot["copyright"] = _decode_exif(exif.get("0th", {}).get(module.piexif.ImageIFD.Copyright, b""))
    except Exception as exc:  # Fixture snapshot should never hide a failed read.
        snapshot["exif_read_error"] = type(exc).__name__
    return snapshot


def _run_worker(module, items, output_dir, creator="Current Creator"):
    harness = _WorkerHarness()
    module.App._save_worker(harness, items, output_dir=output_dir, creator=creator, country="Testland", date_created="2026-08-02")
    assert harness.done is not None
    return harness.done


def test_jpeg_default_save_keeps_same_named_source_and_writes_new_output(app_module, tmp_path):
    source = tmp_path / "Same_Name_Title.jpg"
    _make_jpeg(source, app_module)
    before = _snapshot(source, app_module)
    card = object()
    result = _run_worker(
        app_module,
        [{"card": card, "path": str(source), **_synth_result()}],
        output_dir=None,
    )
    after = _snapshot(source, app_module)
    output = tmp_path / "Same_Name_Title_1.jpg"
    output_snapshot = _snapshot(output, app_module)

    assert result["ok"] == 1
    assert result["errors"] == []
    assert result["saved_paths"] == [str(output)]
    assert after["sha256"] == before["sha256"]
    assert output_snapshot["format"] == "JPEG"
    assert output_snapshot["icc_present"] is True
    assert output_snapshot["creator"] == "Current Creator"


def test_save_to_folder_preserves_source_but_rewrites_output_metadata_and_profile(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    before = _snapshot(source, app_module)
    result = _run_worker(
        app_module,
        [{"card": object(), "path": str(source), **_synth_result()}],
        output_dir=output_dir,
    )
    output = output_dir / "Same_Name_Title.jpg"
    after_source = _snapshot(source, app_module)
    output_snapshot = _snapshot(output, app_module)

    assert result["ok"] == 1
    assert after_source["sha256"] == before["sha256"]
    assert output_snapshot["format"] == "JPEG"
    assert output_snapshot["icc_present"] is True
    assert output_snapshot["has_xmp"] is True
    assert output_snapshot["creator"] == "Current Creator"


def test_inject_metadata_merges_existing_xmp_and_iptc_without_dropping_unmanaged_data(app_module, tmp_path):
    target = tmp_path / "legacy.jpg"
    _make_jpeg(target, app_module)
    before = _snapshot(target, app_module)
    injected = app_module.inject_metadata(
        str(target),
        "Replacement Title",
        "Replacement Description",
        ["replacement", "metadata", "test"],
        creator="Replacement Creator",
    )
    output = tmp_path / "injected.jpg"
    output.write_bytes(injected)
    after = _snapshot(output, app_module)
    raw = output.read_bytes()

    assert before["legacy_xmp_marker"] is True
    assert before["legacy_iptc_marker"] is True
    assert _snapshot(target, app_module) == before
    assert after["legacy_xmp_marker"] is True
    assert after["legacy_iptc_marker"] is True
    assert b"Replacement Title" in raw
    assert after["has_xmp"] is True
    assert after["creator"] == "Replacement Creator"
    assert after["copyright"] == "Legacy Copyright"


def test_png_and_webp_preserve_container_format(app_module, tmp_path):
    makers = {
        "png": ("alpha.png", _make_png, ".png", "PNG", True, True),
        "webp": ("image.webp", _make_webp, ".webp", "WEBP", True, True),
    }
    for label, (name, maker, suffix, expected_format, has_alpha, has_icc) in makers.items():
        source = tmp_path / name
        output_dir = tmp_path / f"output_{label}"
        output_dir.mkdir()
        maker(source)
        before = _snapshot(source, app_module)
        title = f"{label} conversion title"
        item = {
            "card": object(),
            "path": str(source),
            "title": title,
            "description": "Synthetic format conversion fixture for isolated runtime testing.",
            "keywords": [label, "conversion", "test"],
        }
        result = _run_worker(app_module, [item], output_dir=output_dir)
        output = output_dir / app_module.build_output_filename(title, suffix=suffix)
        after_source = _snapshot(source, app_module)
        output_snapshot = _snapshot(output, app_module)

        assert result["ok"] == 1
        assert after_source["sha256"] == before["sha256"]
        assert output.suffix.lower() == suffix
        assert output_snapshot["format"] == expected_format
        assert output_snapshot["has_alpha"] is has_alpha
        assert output_snapshot["icc_present"] is has_icc


def test_save_to_folder_uses_suffix_when_output_name_conflicts(app_module, tmp_path):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    title = "Collision Name"
    collision = output_dir / app_module.build_output_filename(title)
    collision.write_bytes(b"existing synthetic output")
    item = {
        "card": object(),
        "path": str(source),
        "title": title,
        "description": "Synthetic collision test output.",
        "keywords": ["collision", "test", "output"],
    }

    result = _run_worker(app_module, [item], output_dir=output_dir)

    assert result["ok"] == 1
    assert collision.read_bytes() == b"existing synthetic output"
    assert (output_dir / "Collision_Name_1.jpg").exists()


def test_metadata_staging_failure_is_reported_as_save_failure(app_module, tmp_path, monkeypatch):
    source = tmp_path / "source.jpg"
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    _make_jpeg(source, app_module)
    title = "Failed Injection Title"
    target = output_dir / app_module.build_output_filename(title)
    def fail_metadata_stage(*_args, **_kwargs):
        raise PermissionError("synthetic metadata staging failure")

    monkeypatch.setattr(app_module.metadata, "_write_metadata_to_tempfile", fail_metadata_stage)
    item = {
        "card": object(),
        "path": str(source),
        "title": title,
        "description": "Synthetic write-failure test output.",
        "keywords": ["failure", "metadata", "test"],
    }
    result = _run_worker(app_module, [item], output_dir=output_dir)

    assert result["ok"] == 0
    assert result["saved_cards"] == []
    assert result["errors"]
    assert not target.exists()
    assert not list(output_dir.glob(".pixtagra_*.tmp"))


def test_vault_direct_read_rederivation_corruption_and_missing_salt_are_isolated(app_module):
    assert app_module.save_vault({"gemini": TEST_KEY}) is True
    assert app_module.load_vault() == {"gemini": TEST_KEY}

    app_module._cached_vault_key = None
    first = app_module._derive_vault_key()
    app_module._cached_vault_key = None
    second = app_module._derive_vault_key()
    assert first == second

    # No Windows Hello function is called by direct module-level vault reads.
    app_module.verify_windows_identity = lambda: (_ for _ in ()).throw(AssertionError("must not be called"))
    assert app_module.load_vault() == {"gemini": TEST_KEY}

    app_module.VAULT_FILE.write_bytes(b"corrupt synthetic vault")
    app_module._cached_vault_key = None
    assert app_module.load_vault() == {}

    assert app_module.save_vault({"gemini": TEST_KEY}) is True
    original_vault = app_module.VAULT_FILE.read_bytes()
    app_module.SALT_FILE.unlink()
    app_module._cached_vault_key = None
    assert app_module.load_vault() == {}
    assert app_module.VAULT_FILE.read_bytes() == original_vault


def _legacy_config_app(module):
    app = _bare_app(module, key="")
    app.creator_var = _Var()
    app.shoot_location_var = _Var()
    app.shoot_date_var = _Var()
    app.base_url_var = _Var()
    app.local_url_var = _Var("http://localhost:11434")
    app.model_var = _Var(module.SELECTABLE_MODELS[0])
    app.mode_var = _Var(module.PROMPT_MODES[0])
    return app


def test_legacy_config_is_an_explicit_migration_source_not_a_daily_vault_write(app_module, tmp_path):
    config = tmp_path / "legacy-success.json"
    config.write_text(json.dumps({"api_key": TEST_KEY, "model": "Google Gemini 2.5 Flash", "mode": app_module.PROMPT_MODES[0]}), encoding="utf-8")
    app_module.CONFIG_FILE = config

    app = _legacy_config_app(app_module)
    app._load_config()

    assert json.loads(config.read_text(encoding="utf-8"))["api_key"] == TEST_KEY
    assert app._legacy_config_key_present is True
    assert app.key_var.get() == ""
    assert app.model_var.get() == app_module.SELECTABLE_MODELS[0]
    assert app_module.API_CONFIG[app.model_var.get()]["provider"] != "gemini"
    assert app_module.load_vault() == {}


def test_legacy_config_vault_save_failure_preserves_config_key(app_module, tmp_path, monkeypatch, capsys):
    config = tmp_path / "legacy-fail.json"
    config.write_text(json.dumps({"api_key": TEST_KEY, "model": "Google Gemini 2.5 Flash", "mode": app_module.PROMPT_MODES[0]}), encoding="utf-8")
    app_module.CONFIG_FILE = config
    monkeypatch.setattr(app_module, "_load_vault_for_legacy_migration", lambda: {})
    monkeypatch.setattr(app_module, "save_vault", lambda _keys: False)

    app = _legacy_config_app(app_module)
    app._load_config()

    assert json.loads(config.read_text(encoding="utf-8"))["api_key"] == TEST_KEY
    assert TEST_KEY not in capsys.readouterr().out
    assert TEST_KEY not in capsys.readouterr().err


def test_legacy_config_write_failure_leaves_synthetic_source_key(app_module, tmp_path, monkeypatch):
    config = tmp_path / "legacy-write-fail.json"
    original = {"api_key": TEST_KEY, "model": "Google Gemini 2.5 Flash", "mode": app_module.PROMPT_MODES[0]}
    config.write_text(json.dumps(original), encoding="utf-8")
    app_module.CONFIG_FILE = config
    original_fdopen = app_module.os.fdopen

    def fail_config_write(fd, mode="r", *args, **kwargs):
        if "w" in mode:
            raise OSError("synthetic config write failure")
        return original_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(app_module.os, "fdopen", fail_config_write)
    app = _legacy_config_app(app_module)
    app._load_config()

    assert json.loads(config.read_text(encoding="utf-8"))["api_key"] == TEST_KEY
    assert app._legacy_config_key_present is True
    assert app_module.load_vault() == {}


def test_batch_never_automatically_persists_working_synthetic_key(app_module, monkeypatch):
    app = _bare_app(app_module)
    app.stop_flag = False
    app.error_lock = threading.Lock()
    app.api_errors = []
    app.stats = {"total": 1, "processed": 0, "success": 0, "need_review": 0, "failed": 0}
    app.after = lambda _delay, callback: callback()
    app._finish = lambda: None
    app._process = lambda *_args: _synth_result()

    def record(_card, result, _total, _mode):
        app.stats["processed"] += 1
        app.stats["success"] += int(bool(result))

    app._record_analysis_result = record
    saved = []
    monkeypatch.setattr(app_module, "save_vault", lambda keys: saved.append(dict(keys)) or True)
    monkeypatch.setattr(app_module.time, "sleep", lambda _seconds: None)
    lease = type("Lease", (), {"secret_for_authorized_use": lambda self: TEST_KEY})()

    app_module.App._analyze_thread(app, [object()], "Google Gemini 2.5 Flash", app_module.PROMPT_MODES[0], lease, "", "")

    assert saved == []


def test_non_rate_limited_batch_does_not_serially_throttle_executor_submissions(app_module):
    app = _bare_app(app_module)
    app.stop_flag = False
    app.error_lock = threading.Lock()
    app.api_errors = []
    app.stats = {"total": 100, "processed": 0, "success": 0, "need_review": 0, "failed": 0}
    app.after = lambda _delay, callback: callback()
    app._finish = lambda: None
    app._process = lambda *_args: _synth_result()

    received_modes = []

    def record(_card, result, _total, mode):
        app.stats["processed"] += 1
        app.stats["success"] += int(bool(result))
        received_modes.append(mode)

    app._record_analysis_result = record
    started = time.monotonic()

    app_module.App._analyze_thread(
        app,
        list(range(100)),
        "Google Gemini 2.5 Flash",
        app_module.PROMPT_MODES[0],
        None,
        "",
        "",
    )

    assert app.stats["processed"] == 100
    assert app.stats["success"] == 100
    assert received_modes == [app_module.PROMPT_MODES[0]] * 100
    assert time.monotonic() - started < 2


def test_analysis_worker_queues_results_without_calling_tk(app_module):
    app = _bare_app(app_module)
    app.stop_flag = False
    app._analysis_result_queue = app_module.queue.Queue()
    app._process = lambda *_args: _synth_result()
    app.after = lambda *_args: (_ for _ in ()).throw(AssertionError("worker called Tk"))

    app_module.App._analyze_thread(
        app,
        list(range(100)),
        "Google Gemini 2.5 Flash",
        app_module.PROMPT_MODES[0],
        None,
        "",
        "",
    )

    queued = []
    while not app._analysis_result_queue.empty():
        queued.append(app._analysis_result_queue.get_nowait())
    assert [item[0] for item in queued] == ["result"] * 100 + ["finish"]
    assert all(item[4] == app_module.PROMPT_MODES[0] for item in queued[:-1])


def test_cancel_does_not_abort_active_mock_request(app_module, monkeypatch):
    app = _bare_app(app_module)
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Response:
        status_code = 200
        text = "{}"
        headers = {}

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(_synth_result())}]}}]}

    class Session:
        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            calls.append("post")
            started.set()
            assert release.wait(3)
            return Response()

    monkeypatch.setattr(app_module.requests, "Session", Session)
    worker = threading.Thread(target=lambda: app._call_api("abc", model_name="Google Gemini 2.5 Flash", api_key=TEST_KEY))
    worker.start()
    assert started.wait(3)
    app._stop()
    assert app.stop_flag is True
    assert worker.is_alive()
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert calls == ["post"]


def test_cancel_prevents_queued_requests_and_further_retries(app_module, monkeypatch):
    app = _bare_app(app_module)
    app.stop_flag = False
    app.error_lock = threading.Lock()
    app.api_errors = []
    app.stats = {"total": 3, "processed": 0, "success": 0, "need_review": 0, "failed": 0}
    app.after = lambda _delay, callback: callback()
    app._finish = lambda: None
    monkeypatch.setattr(app_module, "HAS_CRYPTO", False)
    monkeypatch.setattr(app_module.time, "sleep", lambda _seconds: None)

    started_two = threading.Event()
    release = threading.Event()
    all_submitted = threading.Event()
    calls = []
    lock = threading.Lock()

    class TrackingExecutor:
        def __init__(self, max_workers):
            from concurrent.futures import ThreadPoolExecutor

            self.inner = ThreadPoolExecutor(max_workers=max_workers)
            self.submitted = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.inner.shutdown(wait=True)

        def submit(self, fn, *args):
            self.submitted += 1
            if self.submitted == 3:
                all_submitted.set()
            return self.inner.submit(fn, *args)

    def process(card, *_args):
        with lock:
            calls.append(card)
            if len(calls) >= 2:
                started_two.set()
        assert release.wait(3)
        return _synth_result()

    def record(_card, result, _total, _mode):
        app.stats["processed"] += 1
        app.stats["success"] += int(bool(result))

    app._process = process
    app._record_analysis_result = record
    monkeypatch.setattr(app_module, "ThreadPoolExecutor", TrackingExecutor)
    lease = type("Lease", (), {"secret_for_authorized_use": lambda self: TEST_KEY})()
    worker = threading.Thread(target=lambda: app_module.App._analyze_thread(app, ["a", "b", "c"], "Google Gemini 2.5 Flash", app_module.PROMPT_MODES[0], lease, "", ""))
    worker.start()
    assert started_two.wait(3)
    assert not all_submitted.is_set()  # Dispatch is bounded to two active requests.
    app._stop()
    assert worker.is_alive()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    # Active HTTP calls may finish, but queued images must not incur new calls.
    assert calls == ["a", "b"]
    assert app.stats["processed"] == 2
    assert app.stats["success"] == 2  # Retain results of both already-paid requests.

    retry_app = _bare_app(app_module)
    retry_app.stop_flag = False
    retry_started = threading.Event()
    retry_release = threading.Event()
    attempts = []

    def fake_once(*_args, **_kwargs):
        attempts.append("call")
        if len(attempts) == 1:
            raise app_module._RetryableApiError("synthetic retry", retry_after=0)
        return _synth_result()

    retry_app._call_api_once = fake_once

    def blocked_sleep(_seconds):
        retry_started.set()
        assert retry_release.wait(3)

    monkeypatch.setattr(app_module.time, "sleep", blocked_sleep)
    retry_worker = threading.Thread(target=lambda: retry_app._call_api("abc", model_name="Google Gemini 2.5 Flash", api_key=TEST_KEY))
    retry_worker.start()
    assert retry_started.wait(3)
    retry_app._stop()
    assert retry_worker.is_alive()
    retry_release.set()
    retry_worker.join(3)
    assert attempts == ["call"]


def test_cancel_does_not_abort_keyword_repair_request(app_module):
    app = _bare_app(app_module)
    app.stop_flag = False
    started = threading.Event()
    release = threading.Event()

    def repair(*_args, **_kwargs):
        started.set()
        assert release.wait(3)
        return []

    app._repair_api_call = repair
    worker = threading.Thread(
        target=lambda: app._apply_sanitizer_and_repair(
            {"title": "Mountain and Lake Landscape beneath Clouds in Evening Light",
             "description": "Mountains rise above a lake with trees along the water and clouds overhead. Evening light falls across the landscape, showing the rocky slopes and the still water in a wide view.",
             "keywords": ["vibes"]},
            "base64",
            {"type": "cloud"},
            TEST_KEY,
            "",
            "",
            "Google Gemini 2.5 Flash",
        )
    )
    worker.start()
    assert started.wait(3)
    app._stop()
    assert worker.is_alive()
    release.set()
    worker.join(3)
    assert not worker.is_alive()
