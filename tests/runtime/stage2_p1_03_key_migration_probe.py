"""Isolated P1-03 legacy API key migration probe with no network access."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.runtime import test_p1_03_safe_key_migration as migration_tests


def _load_isolated_module(root):
    monkeypatch = pytest.MonkeyPatch()
    generator = migration_tests.app_module.__wrapped__(root, monkeypatch)
    module = next(generator)
    return module, monkeypatch, generator


def _close_isolated_module(monkeypatch, generator):
    try:
        next(generator)
    except StopIteration:
        pass
    monkeypatch.undo()


def main():
    with tempfile.TemporaryDirectory(prefix="pixtagra_p1_03_probe_") as temp_dir:
        root = Path(temp_dir)
        module, monkeypatch, generator = _load_isolated_module(root)
        try:
            vault_writes = []
            monkeypatch.setattr(module, "save_vault", lambda values: vault_writes.append(dict(values)) or True)
            success_config = root / "success-config.json"
            original_success = migration_tests._legacy_config(success_config)
            success_app = migration_tests._run_load(module, success_config)
            success_data = json.loads(success_config.read_text(encoding="utf-8"))

            # The UI performs this only after a separately verified Windows
            # Credential Manager write; the probe exercises the atomic source
            # removal half with synthetic data only.
            migrated = {name: value for name, value in original_success.items() if name != "api_key"}
            module._remove_legacy_key_from_config(success_config, migrated, success_config.read_bytes())
            explicit_success = json.loads(success_config.read_text(encoding="utf-8"))

            failure_config = root / "failure-config.json"
            failure_original = migration_tests._legacy_config(failure_config)
            failure_app = migration_tests._run_load(module, failure_config)
            failure_data = json.loads(failure_config.read_text(encoding="utf-8"))

            results = {
                "startup_preserves_config_source": success_data == original_success,
                "startup_does_not_autofill": success_app.key_var.get() == "",
                "explicit_verified_removal": "api_key" not in explicit_success,
                "no_legacy_vault_write": vault_writes == [],
                "failure_config_key_preserved": failure_data == failure_original,
                "failure_diagnostic_redacted": migration_tests.TEST_KEY not in failure_app.__dict__.get("_legacy_key_migration_error", ""),
                "success_is_explicit_only": success_app._legacy_config_key_present is True,
                "isolated_config": module.CONFIG_FILE.parent.resolve().is_relative_to(root.resolve()),
                "isolated_vault": module.VAULT_DIR.resolve().is_relative_to(root.resolve()),
                "network_requests": 0,
                "real_vault_access": 0,
                "real_config_access": 0,
            }
            assert all(value is True or value == 0 for value in results.values())
            print("P1-03 migration probe OK")
            print(json.dumps(results, sort_keys=True))
        finally:
            _close_isolated_module(monkeypatch, generator)


if __name__ == "__main__":
    main()
