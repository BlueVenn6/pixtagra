"""Fake-only contract tests for the P0-01-2 legacy Vault migration engine."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

import pixtagra_credential_migration as migration
import pixtagra_credentials as credentials


TEST_KEY = "TEST_KEY_DO_NOT_USE"
OTHER_KEY = "OTHER_TEST_KEY_DO_NOT_USE"
IDENTITY_USER = "synthetic-user"
IDENTITY_MACHINE = "synthetic-machine"


class FakeCredentialBackend:
    """Provider-only in-memory backend with secret-free repr and call history."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.read_overrides = {}
        self.write_overrides = {}
        self.write_failures = {}
        self.read_calls = []
        self.write_calls = []

    def __repr__(self):
        return f"<FakeCredentialBackend reads={len(self.read_calls)} writes={len(self.write_calls)}>"

    def _result(self, operation, status, provider_id, secret=None, error_code=None):
        return credentials.CredentialResult(
            operation=operation,
            status=status,
            provider_id=provider_id,
            target=credentials.credential_target(provider_id),
            error_code=error_code,
            verified=status in {"found", "stored", "already_present", "deleted"},
            secret=secret,
        )

    def read(self, provider_id):
        self.read_calls.append(provider_id)
        overrides = self.read_overrides.get(provider_id)
        if overrides:
            return overrides.pop(0)
        if provider_id not in self.values:
            return self._result("read", "not_found", provider_id, error_code=credentials.ERROR_NOT_FOUND)
        return self._result("read", "found", provider_id, secret=self.values[provider_id])

    def write(self, provider_id, secret, allow_replace=False):
        self.write_calls.append((provider_id, allow_replace))
        assert allow_replace is False
        overrides = self.write_overrides.get(provider_id)
        if overrides:
            return overrides.pop(0)
        if provider_id in self.write_failures:
            return self._result("write", self.write_failures[provider_id], provider_id, error_code=5)
        if provider_id not in self.values:
            self.values[provider_id] = secret
            return self._result("write", "stored", provider_id)
        if self.values[provider_id] == secret:
            return self._result("write", "already_present", provider_id)
        return self._result("write", "conflict", provider_id)


class FailingFileOps(migration.LocalMigrationFileOps):
    def __init__(self, final_path, failure, formal_failure_replace=1):
        super().__init__()
        self.final_path = Path(final_path)
        self.failure = failure
        self.replaced = False
        self.replace_count = 0
        self.formal_failure_replace = formal_failure_replace

    def open_exclusive(self, path):
        if self.failure == "create":
            raise OSError("synthetic create failure")
        return super().open_exclusive(path)

    def open_text(self, fd):
        handle = super().open_text(fd)
        if self.failure in {"write", "flush"}:
            return _FailingHandle(handle, self.failure)
        return handle

    def fsync(self, fd):
        if self.failure == "fsync":
            raise OSError("synthetic fsync failure")
        return super().fsync(fd)

    def replace(self, source, target):
        if self.failure == "replace":
            raise OSError("synthetic replace failure")
        super().replace(source, target)
        self.replaced = True
        self.replace_count += 1

    def read_bytes(self, path):
        path = Path(path)
        if self.failure == "temp_readback" and path != self.final_path:
            raise OSError("synthetic temp readback failure")
        if (
            self.failure == "formal_readback"
            and path == self.final_path
            and self.replace_count >= self.formal_failure_replace
        ):
            raise OSError("synthetic formal readback failure")
        return super().read_bytes(path)


class _FailingHandle:
    def __init__(self, handle, failure):
        self.handle = handle
        self.failure = failure

    def write(self, text):
        if self.failure == "write":
            raise OSError("synthetic write failure")
        return self.handle.write(text)

    def flush(self):
        if self.failure == "flush":
            raise OSError("synthetic flush failure")
        return self.handle.flush()

    def fileno(self):
        return self.handle.fileno()

    def close(self):
        return self.handle.close()


class DeleteFailureOps(migration.LocalMigrationFileOps):
    def __init__(self, fail_name, leave_present=False):
        super().__init__()
        self.fail_name = fail_name
        self.leave_present = leave_present

    def unlink(self, path):
        path = Path(path)
        if path.name == self.fail_name:
            if self.leave_present:
                return None
            raise OSError("synthetic delete failure")
        return super().unlink(path)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = tmp_path / "config"
    for path in (home, config):
        path.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PSS_CONFIG_DIR", str(config))
    monkeypatch.setenv("PSS_CONFIG_FILE", str(config / "config.json"))


@pytest.fixture
def context(tmp_path):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    source = migration.LegacyVaultSource(
        vault_path=vault_dir / "keys.vault",
        salt_path=vault_dir / "keys.salt",
        username=IDENTITY_USER,
        machine_name=IDENTITY_MACHINE,
    )
    ops = migration.LocalMigrationFileOps()
    store = migration.MigrationStateStore(vault_dir / "credential-migration-state.json", file_ops=ops)
    return {"source": source, "ops": ops, "store": store, "vault_dir": vault_dir}


def _derive_key(source, username=IDENTITY_USER, machine_name=IDENTITY_MACHINE):
    seed = f"{username}@{machine_name}".encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", seed, source.salt_path.read_bytes(), 100_000, dklen=32)


def _write_plaintext(source, plaintext, username=IDENTITY_USER, machine_name=IDENTITY_MACHINE):
    source.salt_path.write_bytes(b"S" * 32)
    nonce = b"N" * 12
    ciphertext = AESGCM(_derive_key(source, username, machine_name)).encrypt(nonce, plaintext, None)
    source.vault_path.write_bytes(nonce + ciphertext)


def _write_vault(source, mapping, username=IDENTITY_USER, machine_name=IDENTITY_MACHINE):
    _write_plaintext(
        source,
        json.dumps(mapping, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
        username=username,
        machine_name=machine_name,
    )


def _engine(context, backend=None, state_store=None, file_ops=None):
    return migration.VaultCredentialMigrationEngine(
        source=context["source"],
        backend=backend or FakeCredentialBackend(),
        state_store=state_store or context["store"],
        file_ops=file_ops or context["ops"],
        migration_id_factory=lambda: "synthetic-migration-id",
    )


def _state(provider_ids=("gemini",), phase="restart_verification_pending"):
    return {
        "schema_version": 1,
        "backend": "windows_credential_manager",
        "migration_id": "synthetic-migration-id",
        "phase": phase,
        "provider_ids": list(provider_ids),
        "provider_statuses": {provider: "stored" for provider in provider_ids},
        "first_write_verified": phase in {"restart_verification_pending", "legacy_cleanup_pending", "legacy_cleanup_complete"},
        "cleanup_pending": phase in {"legacy_cleanup_pending", "legacy_cleanup_complete"},
        "cleanup_complete": phase == "legacy_cleanup_complete",
        "error_stage": None,
        "error_code": None,
    }


@pytest.mark.parametrize(
    "mapping, expected_ids",
    (({"gemini": TEST_KEY}, ("gemini",)), ({"openai": OTHER_KEY, "gemini": TEST_KEY}, ("gemini", "openai"))),
)
def test_strict_decoder_reads_current_legacy_format(mapping, expected_ids, context):
    _write_vault(context["source"], mapping)

    result = migration.StrictLegacyVaultDecoder(context["source"]).read()

    assert result.status == "valid"
    assert result.provider_ids == expected_ids
    assert result.secrets == mapping


@pytest.mark.parametrize(
    "create_vault, create_salt, expected",
    ((False, False, "legacy_not_found"), (False, True, "missing_vault"), (True, False, "missing_salt")),
)
def test_decoder_distinguishes_missing_legacy_inputs(create_vault, create_salt, expected, context):
    if create_vault:
        context["source"].vault_path.write_bytes(b"x" * 28)
    if create_salt:
        context["source"].salt_path.write_bytes(b"S" * 32)

    assert migration.StrictLegacyVaultDecoder(context["source"]).read().status == expected


@pytest.mark.parametrize(
    "mutator, expected",
    (
        (lambda source: source.salt_path.write_bytes(b"S" * 31), "invalid_salt_length"),
        (lambda source: source.vault_path.write_bytes(b"N" * 27), "vault_too_short"),
    ),
)
def test_decoder_rejects_invalid_salt_and_short_ciphertext(mutator, expected, context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    mutator(context["source"])

    assert migration.StrictLegacyVaultDecoder(context["source"]).read().status == expected


@pytest.mark.parametrize(
    "username, machine_name",
    (("wrong-user", IDENTITY_MACHINE), (IDENTITY_USER, "wrong-machine")),
)
def test_decoder_rejects_wrong_identity(username, machine_name, context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    source = migration.LegacyVaultSource(
        context["source"].vault_path, context["source"].salt_path, username, machine_name
    )

    assert migration.StrictLegacyVaultDecoder(source).read().status == "decrypt_failed"


@pytest.mark.parametrize(
    "plaintext, expected",
    (
        (b"\xff", "invalid_utf8"),
        (b"{not json", "invalid_json"),
        (b"[]", "invalid_schema"),
        (b'{"gemini":"one","gemini":"two"}', "duplicate_provider"),
        (b'{"unknown":"value"}', "unsupported_provider"),
        (b'{"ollama":"value"}', "unsupported_provider"),
        (b'{"qwen":"value"}', "unsupported_provider"),
        (b'{"minimax":"value"}', "unsupported_provider"),
        (b'{"gemini":""}', "invalid_secret"),
        (b'{"gemini":null}', "invalid_secret"),
        (b'{"gemini":"a\\u0000b"}', "invalid_secret"),
        (b'{"gemini":{"nested":"value"}}', "invalid_schema"),
    ),
)
def test_decoder_rejects_invalid_plaintext_schema(plaintext, expected, context):
    _write_plaintext(context["source"], plaintext)

    assert migration.StrictLegacyVaultDecoder(context["source"]).read().status == expected


def test_decoder_rejects_overlong_utf8_secret_and_redacts_result(context):
    _write_vault(context["source"], {"gemini": "\u00e9" * 1281})

    result = migration.StrictLegacyVaultDecoder(context["source"]).read()

    assert result.status == "invalid_secret"
    assert TEST_KEY not in repr(result)
    assert TEST_KEY not in str(result)


def test_decoder_rejects_tampered_ciphertext_without_mutating_legacy_files(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    original = context["source"].vault_path.read_bytes()
    context["source"].vault_path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))

    result = migration.StrictLegacyVaultDecoder(context["source"]).read()

    assert result.status == "decrypt_failed"
    assert context["source"].vault_path.read_bytes() != original
    assert context["source"].salt_path.exists()


@pytest.mark.parametrize(
    "value",
    (0, False, [], {"nested": "value"}, "contains\x00nul"),
)
def test_decoder_rejects_each_nonconforming_secret_value(value, context):
    _write_vault(context["source"], {"gemini": value})

    result = migration.StrictLegacyVaultDecoder(context["source"]).read()

    assert result.status in {"invalid_secret", "invalid_schema"}


def test_decoder_failure_never_writes_backend_state_or_deletes_legacy(context):
    _write_plaintext(context["source"], b"not-json")
    backend = FakeCredentialBackend()
    result = _engine(context, backend).first_run()

    assert result.phase == "migration_failed"
    assert backend.write_calls == []
    assert context["source"].vault_path.exists()
    assert context["source"].salt_path.exists()
    assert context["store"].read().status == "not_found"


def test_first_run_migrates_each_provider_but_preserves_legacy_until_restart(context):
    mapping = {"gemini": TEST_KEY, "openai": OTHER_KEY}
    _write_vault(context["source"], mapping)
    backend = FakeCredentialBackend()

    result = _engine(context, backend).first_run()
    state = context["store"].read()

    assert result.phase == "restart_verification_pending"
    assert backend.values == mapping
    assert backend.write_calls == [("gemini", False), ("openai", False)]
    assert context["source"].vault_path.exists()
    assert context["source"].salt_path.exists()
    assert state.status == "valid"
    assert state.state["phase"] == "restart_verification_pending"
    serialized = json.dumps(state.state, sort_keys=True)
    assert TEST_KEY not in serialized
    assert OTHER_KEY not in serialized


def test_first_run_accepts_identical_destination_without_rewrite(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend({"gemini": TEST_KEY})

    result = _engine(context, backend).first_run()

    assert result.phase == "restart_verification_pending"
    assert backend.write_calls == []
    assert context["store"].read().state["provider_statuses"]["gemini"] == "already_present"


@pytest.mark.parametrize("provider_id", migration.LEGACY_VAULT_PROVIDER_IDS)
def test_each_approved_provider_migrates_to_its_own_backend_entry(provider_id, context):
    _write_vault(context["source"], {provider_id: TEST_KEY})
    backend = FakeCredentialBackend()

    result = _engine(context, backend).first_run()

    assert result.phase == "restart_verification_pending"
    assert backend.values == {provider_id: TEST_KEY}
    assert backend.write_calls == [(provider_id, False)]


@pytest.mark.parametrize("status", ("write_failed", "verification_failed", "unavailable"))
def test_first_run_backend_write_failures_do_not_schedule_restart_or_cleanup(status, context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    backend.write_overrides["gemini"] = [
        credentials.CredentialResult(
            operation="write",
            status=status,
            provider_id="gemini",
            target=credentials.credential_target("gemini"),
            verified=False,
            error_code=5,
        )
    ]

    result = _engine(context, backend).first_run()

    assert result.phase == "migration_failed"
    assert context["source"].vault_path.exists()
    assert context["source"].salt_path.exists()
    assert context["store"].read().state["phase"] == "migration_failed"


@pytest.mark.parametrize("status", ("error", "invalid_data", "unavailable"))
def test_first_run_backend_read_failures_do_not_write_or_cleanup(status, context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    backend.read_overrides["gemini"] = [
        credentials.CredentialResult(
            operation="read",
            status=status,
            provider_id="gemini",
            target=credentials.credential_target("gemini"),
            error_code=5,
        )
    ]

    result = _engine(context, backend).first_run()

    assert result.phase == "migration_failed"
    assert backend.write_calls == []
    assert context["source"].vault_path.exists()


def test_first_run_conflict_never_overwrites_or_schedules_cleanup(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend({"gemini": OTHER_KEY})

    result = _engine(context, backend).first_run()
    state = context["store"].read()

    assert result.phase == "conflict"
    assert result.conflict_provider_ids == ("gemini",)
    assert backend.values["gemini"] == OTHER_KEY
    assert backend.write_calls == []
    assert state.state["phase"] == "conflict"
    assert context["source"].vault_path.exists()
    assert context["source"].salt_path.exists()


def test_partial_write_failure_preserves_legacy_and_is_retryable(context):
    _write_vault(context["source"], {"gemini": TEST_KEY, "openai": OTHER_KEY})
    backend = FakeCredentialBackend()
    backend.write_failures["openai"] = "write_failed"

    failed = _engine(context, backend).first_run()

    assert failed.phase == "migration_failed"
    assert backend.values == {"gemini": TEST_KEY}
    assert context["source"].vault_path.exists()
    assert context["store"].read().state["phase"] == "migration_failed"

    backend.write_failures.clear()
    retried = _engine(context, backend).first_run()
    assert retried.phase == "restart_verification_pending"
    assert backend.values == {"gemini": TEST_KEY, "openai": OTHER_KEY}


def test_state_write_failures_preserve_previous_state_and_legacy(context, monkeypatch):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    original = _state(phase="migration_failed")
    assert context["store"].write(original).status == "written"

    for failure in ("create", "write", "flush", "fsync", "replace", "temp_readback", "formal_readback"):
        failing_ops = FailingFileOps(context["store"].path, failure)
        store = migration.MigrationStateStore(context["store"].path, file_ops=failing_ops)
        result = _engine(context, FakeCredentialBackend(), state_store=store).first_run()

        assert result.phase == "migration_failed"
        assert context["source"].vault_path.exists()
        assert json.loads(context["store"].path.read_text(encoding="utf-8")) == original
        assert not list(context["vault_dir"].glob(".pixtagra_migration_*.tmp"))


def test_state_write_round_trip_is_atomic_and_contains_only_nonsecret_schema(context):
    original = _state(("gemini", "openai"))

    result = context["store"].write(original)
    reread = context["store"].read()

    assert result.status == "written"
    assert reread.status == "valid"
    assert reread.state == original
    raw = context["store"].path.read_text(encoding="utf-8")
    for forbidden in (TEST_KEY, OTHER_KEY, IDENTITY_USER, IDENTITY_MACHINE, "keys.salt", "keys.vault"):
        assert forbidden not in raw
    assert not list(context["vault_dir"].glob(".pixtagra_migration_*.tmp"))


@pytest.mark.parametrize(
    "malformed",
    (
        {"schema_version": 1},
        {**_state(), "api_key": TEST_KEY},
        {**_state(), "provider_ids": ["gemini", "gemini"]},
        {**_state(), "provider_statuses": {"unknown": "stored"}},
        {**_state(), "error_stage": "arbitrary secret-like value"},
    ),
)
def test_state_schema_rejects_unknown_or_sensitive_shapes(malformed, context):
    result = context["store"].write(malformed)

    assert result.status == "write_failed"
    assert not context["store"].path.exists()


def test_state_rejects_secret_shaped_or_unknown_schema_without_cleanup(context):
    context["store"].path.write_text(json.dumps({"schema_version": 999, "api_key": TEST_KEY}), encoding="utf-8")
    _write_vault(context["source"], {"gemini": TEST_KEY})

    result = _engine(context).restart_verify_and_cleanup()

    assert result.phase == "migration_failed"
    assert context["source"].vault_path.exists()
    assert TEST_KEY not in repr(result)


@pytest.mark.parametrize(
    "state_content",
    (None, b"{not-json", b'{"schema_version":999}'),
)
def test_restart_requires_a_valid_state_before_any_cleanup(state_content, context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    if state_content is not None:
        context["store"].path.write_bytes(state_content)

    result = _engine(context).restart_verify_and_cleanup()

    assert result.phase == "migration_failed"
    assert context["source"].vault_path.exists()
    assert context["source"].salt_path.exists()


def test_restart_with_missing_vault_and_present_salt_never_deletes_remaining_source(context):
    state = _state(phase="restart_verification_pending")
    assert context["store"].write(state).status == "written"
    context["source"].salt_path.write_bytes(b"S" * 32)

    result = _engine(context, FakeCredentialBackend({"gemini": TEST_KEY})).restart_verify_and_cleanup()

    assert result.phase == "migration_failed"
    assert context["source"].salt_path.exists()


def test_migration_result_repr_is_redacted_even_when_backend_returns_secret(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend({"gemini": OTHER_KEY})

    result = _engine(context, backend).first_run()

    assert result.phase == "conflict"
    assert TEST_KEY not in repr(result)
    assert OTHER_KEY not in repr(result)


def test_restart_verification_compares_fresh_legacy_and_destination_before_cleanup(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    engine = _engine(context, backend)
    assert engine.first_run().phase == "restart_verification_pending"

    result = engine.restart_verify_and_cleanup()

    assert result.phase == "legacy_cleanup_complete"
    assert result.cleanup_complete is True
    assert not context["source"].vault_path.exists()
    assert not context["source"].salt_path.exists()
    assert backend.values["gemini"] == TEST_KEY
    assert context["store"].read().state["phase"] == "legacy_cleanup_complete"


@pytest.mark.parametrize("mode", ("missing", "different", "state_mismatch"))
def test_restart_verification_failure_never_cleans_up(mode, context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    engine = _engine(context, backend)
    assert engine.first_run().phase == "restart_verification_pending"
    if mode == "missing":
        backend.values.clear()
    elif mode == "different":
        backend.values["gemini"] = OTHER_KEY
    else:
        changed = _state(("openai",), phase="restart_verification_pending")
        assert context["store"].write(changed).status == "written"

    result = engine.restart_verify_and_cleanup()

    assert result.phase in {"migration_failed", "conflict"}
    assert context["source"].vault_path.exists()
    assert context["source"].salt_path.exists()


def test_restart_ignores_unrelated_backend_provider_without_touching_it(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend({"zhipu": "UNRELATED_SYNTHETIC", "gemini": TEST_KEY})
    engine = _engine(context, backend)
    assert engine.first_run().phase == "restart_verification_pending"

    result = engine.restart_verify_and_cleanup()

    assert result.cleanup_complete is True
    assert backend.values["zhipu"] == "UNRELATED_SYNTHETIC"
    assert "zhipu" not in backend.read_calls[:-1]


def test_first_run_pending_state_is_idempotent_and_does_not_repeat_writes(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    engine = _engine(context, backend)
    assert engine.first_run().phase == "restart_verification_pending"

    pending = engine.first_run()

    assert pending.phase == "restart_verification_pending"
    assert backend.write_calls == [("gemini", False)]


def test_cleanup_vault_delete_reported_success_but_file_remains_stays_pending(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    engine = _engine(context, backend)
    assert engine.first_run().phase == "restart_verification_pending"
    result = _engine(context, backend, file_ops=DeleteFailureOps("keys.vault", leave_present=True)).restart_verify_and_cleanup()

    assert result.phase == "legacy_cleanup_pending"
    assert result.error_stage == "cleanup_vault_delete"
    assert context["source"].vault_path.exists()
    assert context["source"].salt_path.exists()


def test_cleanup_vault_delete_failure_keeps_salt_and_pending_state(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    engine = _engine(context, backend)
    assert engine.first_run().phase == "restart_verification_pending"
    failure_ops = DeleteFailureOps("keys.vault")
    engine = _engine(context, backend, file_ops=failure_ops)

    result = engine.restart_verify_and_cleanup()

    assert result.phase == "legacy_cleanup_pending"
    assert context["source"].vault_path.exists()
    assert context["source"].salt_path.exists()
    assert context["store"].read().state["phase"] == "legacy_cleanup_pending"


@pytest.mark.parametrize("leave_present", (False, True))
def test_cleanup_salt_failure_is_pending_and_retryable(leave_present, context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    engine = _engine(context, backend)
    assert engine.first_run().phase == "restart_verification_pending"
    failure_ops = DeleteFailureOps("keys.salt", leave_present=leave_present)

    pending = _engine(context, backend, file_ops=failure_ops).restart_verify_and_cleanup()

    assert pending.phase == "legacy_cleanup_pending"
    assert not context["source"].vault_path.exists()
    assert context["source"].salt_path.exists()
    completed = _engine(context, backend).restart_verify_and_cleanup()
    assert completed.phase == "legacy_cleanup_complete"
    assert not context["source"].salt_path.exists()


def test_recovery_after_both_files_deleted_before_complete_state_write(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    engine = _engine(context, backend)
    assert engine.first_run().phase == "restart_verification_pending"
    failing_ops = FailingFileOps(
        context["store"].path, "formal_readback", formal_failure_replace=2
    )
    pending_store = migration.MigrationStateStore(context["store"].path, file_ops=failing_ops)

    result = _engine(context, backend, state_store=pending_store).restart_verify_and_cleanup()

    assert result.phase == "legacy_cleanup_pending"
    assert not context["source"].vault_path.exists()
    assert not context["source"].salt_path.exists()
    recovered = _engine(context, backend).restart_verify_and_cleanup()
    assert recovered.phase == "legacy_cleanup_complete"
    assert backend.values["gemini"] == TEST_KEY


def test_completed_migration_is_idempotent_and_never_recreates_legacy(context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    engine = _engine(context, backend)
    assert engine.first_run().phase == "restart_verification_pending"
    assert engine.restart_verify_and_cleanup().cleanup_complete is True

    again = engine.restart_verify_and_cleanup()

    assert again.phase == "legacy_cleanup_complete"
    assert not context["source"].vault_path.exists()
    assert not context["source"].salt_path.exists()
    assert backend.write_calls == [("gemini", False)]


def test_cleanup_complete_state_does_not_use_legacy_vault_as_fallback(context):
    completed = _state(phase="legacy_cleanup_complete")
    assert context["store"].write(completed).status == "written"
    backend = FakeCredentialBackend({"gemini": TEST_KEY})

    result = _engine(context, backend).first_run()

    assert result.phase == "legacy_cleanup_complete"
    assert backend.read_calls == []
    assert backend.write_calls == []
    assert not context["source"].vault_path.exists()


def test_results_backend_and_state_never_expose_synthetic_secret(capsys, context):
    _write_vault(context["source"], {"gemini": TEST_KEY})
    backend = FakeCredentialBackend()
    result = _engine(context, backend).first_run()
    state_text = context["store"].path.read_text(encoding="utf-8")
    print(result)
    print(backend)
    captured = capsys.readouterr()

    assert TEST_KEY not in repr(result)
    assert TEST_KEY not in str(result)
    assert TEST_KEY not in repr(backend)
    assert TEST_KEY not in state_text
    assert TEST_KEY not in captured.out
