"""P1-03 compatibility after daily credentials moved out of the legacy Vault.

The old P1-03 transaction is still protected as a recovery helper, but startup
must now preserve config ``api_key`` as an explicit migration source instead of
silently writing it to the deprecated Vault.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.runtime.test_stage2_isolation import TEST_KEY, _Var, _bare_app, app_module


OTHER_KEY = "OTHER_TEST_KEY_DO_NOT_USE"


def _legacy_app(module):
    app = _bare_app(module, key="")
    app._legacy_key_migration_error = ""
    app._legacy_config_key_present = False
    app.creator_var = _Var()
    app.shoot_location_var = _Var()
    app.shoot_date_var = _Var()
    app.base_url_var = _Var()
    app.local_url_var = _Var("http://localhost:11434")
    app.model_var = _Var("Google Gemini 2.5 Flash")
    app.mode_var = _Var(module.PROMPT_MODES[0])
    return app


def _legacy_config(path: Path, key=TEST_KEY, **extra):
    data = {
        "api_key": key,
        "model": "Google Gemini 2.5 Flash",
        "mode": "\U0001f4f7 General Photography",
        "creator": "Legacy Creator",
        "shoot_location": "Legacy Location",
        "shoot_date": "2020-01-02",
        "base_url": "https://example.invalid/api",
        "local_url": "http://localhost:11434",
        "custom_setting": {"keep": True},
    }
    data.update(extra)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _run_load(module, config: Path):
    module.CONFIG_FILE = config
    app = _legacy_app(module)
    app._load_config()
    return app


@pytest.mark.parametrize(
    "key,model",
    [
        (TEST_KEY, "Google Gemini 2.5 Flash"),
        (OTHER_KEY, "OpenAI GPT-4o"),
        ("SYNTHETIC_SESSION_SOURCE", "Moonshot Kimi"),
        ("SYNTHETIC_CONFIG_SOURCE", "Zhipu GLM-4V"),
        ("   ", "Google Gemini 2.5 Flash"),
    ],
)
def test_startup_preserves_legacy_config_key_without_vault_access(app_module, tmp_path, monkeypatch, key, model):
    config = tmp_path / "config.json"
    original = _legacy_config(config, key=key, model=model)
    calls = []
    monkeypatch.setattr(app_module, "save_vault", lambda values: calls.append(values) or True)
    monkeypatch.setattr(app_module, "_load_vault_for_legacy_migration", lambda: (_ for _ in ()).throw(AssertionError("no daily vault read")))

    app = _run_load(app_module, config)

    assert json.loads(config.read_text(encoding="utf-8")) == original
    assert "api_key" not in app._config_data
    assert app._legacy_config_key_present is True
    assert app.key_var.get() == ""
    assert calls == []


@pytest.mark.parametrize("key", ({"bad": "type"}, None, 42))
def test_invalid_legacy_config_values_are_never_deleted(app_module, tmp_path, monkeypatch, key):
    config = tmp_path / "config.json"
    original = _legacy_config(config, key=key)
    monkeypatch.setattr(app_module, "save_vault", lambda _values: (_ for _ in ()).throw(AssertionError("no vault write")))

    app = _run_load(app_module, config)

    assert json.loads(config.read_text(encoding="utf-8")) == original
    assert "api_key" not in app._config_data
    assert not app._legacy_config_key_present


def test_legacy_config_source_blocks_ordinary_config_rewrite(app_module, tmp_path):
    config = tmp_path / "config.json"
    original = _legacy_config(config)
    app = _run_load(app_module, config)

    app.creator_var.set("Changed creator")

    assert app._save_config() is False
    assert json.loads(config.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("failure", ("temp", "write", "fsync", "replace", "final_readback"))
def test_explicit_config_removal_failure_preserves_original_source(app_module, tmp_path, monkeypatch, failure):
    config = tmp_path / "config.json"
    original = _legacy_config(config)
    raw = config.read_bytes()
    migrated = {name: value for name, value in original.items() if name != "api_key"}
    if failure == "temp":
        monkeypatch.setattr(app_module.tempfile, "mkstemp", lambda *_a, **_k: (_ for _ in ()).throw(OSError("synthetic")))
    elif failure == "write":
        original_fdopen = app_module.os.fdopen
        monkeypatch.setattr(app_module.os, "fdopen", lambda fd, mode="r", *a, **k: (_ for _ in ()).throw(OSError("synthetic")) if "w" in mode else original_fdopen(fd, mode, *a, **k))
    elif failure == "fsync":
        monkeypatch.setattr(app_module.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("synthetic")))
    elif failure == "replace":
        monkeypatch.setattr(app_module.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("synthetic")))
    else:
        original_load = app_module.json.load
        calls = []
        def fail_final(handle, *args, **kwargs):
            calls.append(handle.name)
            if len(calls) == 2:
                raise ValueError("synthetic")
            return original_load(handle, *args, **kwargs)
        monkeypatch.setattr(app_module.json, "load", fail_final)

    with pytest.raises(app_module._LegacyKeyMigrationError):
        app_module._remove_legacy_key_from_config(config, migrated, raw)
    assert config.read_bytes() == raw


def test_explicit_verified_config_removal_keeps_nonsecret_settings(app_module, tmp_path):
    config = tmp_path / "config.json"
    original = _legacy_config(config)
    raw = config.read_bytes()
    migrated = {name: value for name, value in original.items() if name != "api_key"}

    app_module._remove_legacy_key_from_config(config, migrated, raw)

    assert json.loads(config.read_text(encoding="utf-8")) == migrated


def test_direct_legacy_helper_still_preserves_source_when_vault_write_fails(app_module, tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    original = _legacy_config(config)
    monkeypatch.setattr(app_module, "_load_vault_for_legacy_migration", lambda: {})
    monkeypatch.setattr(app_module, "save_vault", lambda _values: False)

    with pytest.raises(app_module._LegacyKeyMigrationError):
        app_module._migrate_legacy_api_key(config, original, config.read_bytes())
    assert json.loads(config.read_text(encoding="utf-8")) == original


def test_removed_glm_model_remains_an_explicit_legacy_migration_source(app_module):
    assert app_module._legacy_config_provider_id("Zhipu GLM-4V") == "zhipu"
    assert app_module._legacy_config_provider_id("not-a-model") == ""
