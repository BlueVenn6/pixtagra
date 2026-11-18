"""Run P0-01-2 migration scenarios with temporary files and a fake backend only."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pixtagra_credential_migration as migration
import pixtagra_credentials as credentials


TEST_KEY = "TEST_KEY_DO_NOT_USE"
OTHER_KEY = "OTHER_TEST_KEY_DO_NOT_USE"
USERNAME = "probe-user"
MACHINE_NAME = "probe-machine"


class FakeBackend:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.fail_once_for = set()
        self.calls = []

    def read(self, provider_id):
        self.calls.append(("read", provider_id))
        if provider_id not in self.values:
            return credentials.CredentialResult(
                "read", "not_found", provider_id, credentials.credential_target(provider_id),
                error_code=credentials.ERROR_NOT_FOUND,
            )
        return credentials.CredentialResult(
            "read", "found", provider_id, credentials.credential_target(provider_id),
            verified=True, secret=self.values[provider_id],
        )

    def write(self, provider_id, secret, allow_replace=False):
        self.calls.append(("write", provider_id, allow_replace))
        assert allow_replace is False
        if provider_id in self.fail_once_for:
            self.fail_once_for.remove(provider_id)
            return credentials.CredentialResult(
                "write", "write_failed", provider_id, credentials.credential_target(provider_id), error_code=5,
            )
        if provider_id not in self.values:
            self.values[provider_id] = secret
            return credentials.CredentialResult(
                "write", "stored", provider_id, credentials.credential_target(provider_id), verified=True,
            )
        if self.values[provider_id] == secret:
            return credentials.CredentialResult(
                "write", "already_present", provider_id, credentials.credential_target(provider_id), verified=True,
            )
        return credentials.CredentialResult(
            "write", "conflict", provider_id, credentials.credential_target(provider_id), verified=True,
        )


def write_legacy_vault(source, values):
    source.salt_path.write_bytes(b"S" * 32)
    seed = f"{USERNAME}@{MACHINE_NAME}".encode("utf-8")
    key = hashlib.pbkdf2_hmac("sha256", seed, source.salt_path.read_bytes(), 100_000, dklen=32)
    nonce = b"N" * 12
    plaintext = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    source.vault_path.write_bytes(nonce + AESGCM(key).encrypt(nonce, plaintext, None))


def make_engine(root, backend):
    vault_dir = root / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    source = migration.LegacyVaultSource(
        vault_dir / "keys.vault", vault_dir / "keys.salt", USERNAME, MACHINE_NAME
    )
    state = migration.MigrationStateStore(vault_dir / migration.STATE_FILENAME)
    engine = migration.VaultCredentialMigrationEngine(
        source, backend, state, migration_id_factory=lambda: "probe-migration"
    )
    return source, state, engine


def main():
    with tempfile.TemporaryDirectory(prefix="pixtagra_credential_migration_") as root_text:
        root = Path(root_text)
        home = root / "home"
        config = root / "config"
        home.mkdir()
        config.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        os.environ["PSS_CONFIG_DIR"] = str(config)
        os.environ["PSS_CONFIG_FILE"] = str(config / "config.json")

        backend = FakeBackend()
        source, state, engine = make_engine(root / "success", backend)
        write_legacy_vault(source, {"gemini": TEST_KEY})
        first = engine.first_run()
        assert first.phase == "restart_verification_pending"
        assert source.vault_path.exists() and source.salt_path.exists()
        restart = engine.restart_verify_and_cleanup()
        assert restart.cleanup_complete is True
        assert not source.vault_path.exists() and not source.salt_path.exists()
        assert backend.read("gemini").status == "found"
        assert TEST_KEY not in state.path.read_text(encoding="utf-8")

        conflict_backend = FakeBackend({"gemini": OTHER_KEY})
        conflict_source, _conflict_state, conflict_engine = make_engine(root / "conflict", conflict_backend)
        write_legacy_vault(conflict_source, {"gemini": TEST_KEY})
        conflict = conflict_engine.first_run()
        assert conflict.phase == "conflict"
        assert conflict_backend.values["gemini"] == OTHER_KEY
        assert conflict_source.vault_path.exists() and conflict_source.salt_path.exists()

        retry_backend = FakeBackend()
        retry_backend.fail_once_for.add("openai")
        retry_source, _retry_state, retry_engine = make_engine(root / "retry", retry_backend)
        write_legacy_vault(retry_source, {"gemini": TEST_KEY, "openai": OTHER_KEY})
        failed = retry_engine.first_run()
        retried = retry_engine.first_run()
        assert failed.phase == "migration_failed"
        assert retried.phase == "restart_verification_pending"

        print(json.dumps({
            "network_requests": 0,
            "real_credential_manager_access": 0,
            "real_vault_access": 0,
            "cleanup_complete": True,
            "operations": {
                "first_run": first.phase,
                "restart": restart.phase,
                "conflict": conflict.phase,
                "retry_first": failed.phase,
                "retry_second": retried.phase,
            },
            "temporary_paths": {
                "home": str(home),
                "config": str(config),
                "vault": str(source.vault_path.parent),
            },
        }, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
