"""Fake-only tests for the P0-01-3C1 credential policy coordinator.

No test creates a native backend, shows Windows Hello, uses a clipboard, or
reads a real Vault/config file.  Synthetic secrets are never put in an assert
message or a repr-visible object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import pickle
import threading

import pytest

import pixtagra_credential_policy as policy


TEST_SECRET = "TEST_KEY_DO_NOT_USE"
OTHER_SECRET = "OTHER_TEST_KEY_DO_NOT_USE"


@dataclass(frozen=True)
class FakeResult:
    status: str
    error_type: str | None = None
    verified: bool = False
    secret: str | None = field(default=None, repr=False, compare=False)
    phase: str | None = None
    retryable: bool = True


class FakeHelloBackend:
    def __init__(self, *, availability="available", verifications=None):
        self.availability = availability
        self.verifications = list(verifications or ["verified"])
        self.check_calls = []
        self.request_calls = []

    def __repr__(self):
        return f"<FakeHelloBackend availability={len(self.check_calls)} verification={len(self.request_calls)}>"

    def check_availability(self, hwnd):
        self.check_calls.append(hwnd)
        return FakeResult(self.availability)

    def request_verification(self, hwnd, message):
        self.request_calls.append((hwnd, "message_received"))
        status = self.verifications.pop(0) if self.verifications else "verified"
        return FakeResult(status)


class FakeCredentialBackend:
    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.read_calls = []
        self.write_calls = []
        self.delete_calls = []
        self.read_plan = []
        self.write_plan = []
        self.delete_plan = []

    def __repr__(self):
        return f"<FakeCredentialBackend reads={len(self.read_calls)} writes={len(self.write_calls)} deletes={len(self.delete_calls)}>"

    def read(self, provider_id):
        self.read_calls.append(provider_id)
        if self.read_plan:
            return self.read_plan.pop(0)
        if provider_id not in self.entries:
            return FakeResult("not_found")
        return FakeResult("found", verified=True, secret=self.entries[provider_id])

    def write(self, provider_id, secret, allow_replace=False):
        self.write_calls.append((provider_id, bool(allow_replace)))
        result = self.write_plan.pop(0) if self.write_plan else None
        if result is not None:
            return result
        if provider_id in self.entries and self.entries[provider_id] == secret:
            return FakeResult("already_present", verified=True)
        if provider_id in self.entries and self.entries[provider_id] != secret and not allow_replace:
            return FakeResult("conflict", verified=True)
        self.entries[provider_id] = secret
        return FakeResult("stored", verified=True)

    def delete(self, provider_id):
        self.delete_calls.append(provider_id)
        result = self.delete_plan.pop(0) if self.delete_plan else None
        if result is not None:
            return result
        if provider_id not in self.entries:
            return FakeResult("already_absent", verified=True)
        self.entries.pop(provider_id)
        return FakeResult("deleted", verified=True)


class FakeMigrationEngine:
    def __init__(self, *, first=None, restart=None):
        self.first = first or FakeResult("ok", phase="restart_verification_pending")
        self.restart = restart or FakeResult("ok", phase="legacy_cleanup_complete")
        self.first_calls = 0
        self.restart_calls = 0

    def __repr__(self):
        return f"<FakeMigrationEngine first={self.first_calls} restart={self.restart_calls}>"

    def first_run(self):
        self.first_calls += 1
        return self.first

    def restart_verify_and_cleanup(self):
        self.restart_calls += 1
        return self.restart


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = tmp_path / "config"
    vault = tmp_path / "vault"
    for path in (home, config, vault):
        path.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PSS_CONFIG_DIR", str(config))
    monkeypatch.setenv("PSS_CONFIG_FILE", str(config / "config.json"))
    yield {"home": home, "config": config, "vault": vault}


def _coordinator(*, hello=None, backend=None, migration=None, events=None, thread_checker=None):
    hello = hello or FakeHelloBackend()
    backend = backend or FakeCredentialBackend()
    migration = migration or FakeMigrationEngine()
    coordinator = policy.CredentialPolicyCoordinator(
        hello_backend=hello,
        credential_backend=backend,
        migration_engine=migration,
        thread_checker=thread_checker,
        event_sink=events.append if events is not None else None,
    )
    return coordinator, hello, backend, migration


def test_policy_coordinator_contract_is_available():
    assert policy.CredentialPolicyCoordinator is not None
    assert set(policy.APPROVED_PROVIDER_IDS) == {"gemini", "openai", "kimi", "qwen", "minimax", "zhipu"}


@pytest.mark.parametrize("provider_id", ("ollama", "unknown", "gemini/other", "", None))
def test_session_store_rejects_unapproved_providers(provider_id):
    coordinator, hello, backend, _migration = _coordinator()

    result = coordinator.set_session_key(provider_id, TEST_SECRET)

    assert result.status == "invalid_input"
    assert hello.request_calls == []
    assert backend.write_calls == []


@pytest.mark.parametrize("secret", ("", None, 42, "contains\x00nul", "a" * 2561, "\u00e9" * 1281))
def test_session_store_rejects_invalid_secrets_without_side_effects(secret):
    coordinator, hello, backend, _migration = _coordinator()

    result = coordinator.set_session_key("gemini", secret)

    assert result.status == "invalid_input"
    assert hello.request_calls == []
    assert backend.write_calls == []


def test_session_key_is_memory_only_and_precedes_saved_key(isolated_environment):
    coordinator, hello, backend, _migration = _coordinator(backend=FakeCredentialBackend({"gemini": OTHER_SECRET}))
    before = {name: sorted(path.name for path in value.iterdir()) for name, value in isolated_environment.items()}

    assert coordinator.set_session_key("gemini", TEST_SECRET).status == "session_key_set"
    result = coordinator.authorize_for_batch("gemini", 100)

    assert result.status == "authorized"
    assert result.used_session_key is True
    assert result.lease is not None and result.lease._matches_for_testing(TEST_SECRET)
    assert hello.check_calls == []
    assert hello.request_calls == []
    assert backend.read_calls == []
    after = {name: sorted(path.name for path in value.iterdir()) for name, value in isolated_environment.items()}
    assert after == before


def test_session_keys_clear_individually_and_globally():
    coordinator, _hello, _backend, _migration = _coordinator()
    coordinator.set_session_key("gemini", TEST_SECRET)
    coordinator.set_session_key("openai", OTHER_SECRET)

    assert coordinator.clear_session_key("gemini").status == "session_key_cleared"
    assert not coordinator.has_session_key("gemini")
    assert coordinator.has_session_key("openai")
    assert coordinator.clear_all_session_keys().status == "session_keys_cleared"
    assert not coordinator.has_session_key("openai")


def test_lease_is_redacted_unpicklable_and_invalidates():
    coordinator, _hello, _backend, _migration = _coordinator()
    coordinator.set_session_key("gemini", TEST_SECRET)
    lease = coordinator.authorize_for_batch("gemini", 100).lease

    assert lease is not None
    assert TEST_SECRET not in repr(lease)
    with pytest.raises(TypeError):
        pickle.dumps(lease)
    assert lease._matches_for_testing(TEST_SECRET)
    lease.close()
    assert lease.secret_for_authorized_use() is None


def test_one_shot_management_lease_expires_after_use_without_secret_in_repr():
    coordinator, _hello, _backend, _migration = _coordinator(backend=FakeCredentialBackend({"gemini": TEST_SECRET}))
    result = coordinator.reveal_persistent_key("gemini", 100)

    assert result.status == "authorized"
    assert result.lease is not None and result.lease._matches_for_testing(TEST_SECRET)
    assert TEST_SECRET not in repr(result)
    assert result.lease.secret_for_authorized_use() is not None
    assert result.lease.secret_for_authorized_use() is None


def test_first_stored_use_requires_verified_hello_then_reuses_process_consent():
    coordinator, hello, backend, _migration = _coordinator(backend=FakeCredentialBackend({"gemini": TEST_SECRET}))

    first = coordinator.authorize_for_batch("gemini", 100)
    second = coordinator.authorize_for_batch("gemini", 100)

    assert first.status == "authorized" and first.lease is not None
    assert second.status == "authorized" and second.lease is not None
    assert len(hello.check_calls) == 1
    assert len(hello.request_calls) == 1
    assert backend.read_calls == ["gemini", "gemini"]
    assert coordinator.stored_use_consent_granted is True


def test_new_coordinator_requires_new_process_only_stored_use_consent():
    backend = FakeCredentialBackend({"gemini": TEST_SECRET})
    first, hello_one, _backend_one, migration = _coordinator(backend=backend)
    second, hello_two, _backend_two, _migration_two = _coordinator(backend=backend, migration=migration)

    assert first.authorize_for_batch("gemini", 100).status == "authorized"
    assert second.authorize_for_batch("gemini", 100).status == "authorized"
    assert len(hello_one.request_calls) == 1
    assert len(hello_two.request_calls) == 1


@pytest.mark.parametrize("status", ("canceled", "retries_exhausted", "device_busy", "device_not_present", "not_configured_for_user", "disabled_by_policy", "timeout", "window_destroyed", "interop_error", "error"))
def test_non_verified_stored_use_never_reads_or_grants_session_consent(status):
    coordinator, hello, backend, _migration = _coordinator(
        hello=FakeHelloBackend(verifications=[status]),
        backend=FakeCredentialBackend({"gemini": TEST_SECRET}),
    )

    result = coordinator.authorize_for_batch("gemini", 100)

    assert result.status == "consent_not_verified"
    assert result.hello_status == status
    assert result.persistent_operations_allowed is False
    assert result.session_only_allowed is True
    assert backend.read_calls == []
    assert coordinator.stored_use_consent_granted is False
    assert len(hello.request_calls) == 1


@pytest.mark.parametrize("availability", ("device_not_present", "not_configured_for_user", "disabled_by_policy", "device_busy", "unsupported_platform", "interop_unavailable", "error"))
def test_unavailable_hello_allows_session_only_but_never_reads_stored_key(availability):
    coordinator, hello, backend, _migration = _coordinator(
        hello=FakeHelloBackend(availability=availability),
        backend=FakeCredentialBackend({"gemini": TEST_SECRET}),
    )

    result = coordinator.authorize_for_batch("gemini", 100)

    assert result.status == "hello_unavailable"
    assert result.hello_status == availability
    assert result.persistent_operations_allowed is False
    assert result.session_only_allowed is True
    assert hello.request_calls == []
    assert backend.read_calls == []


def test_verified_stored_use_credential_missing_is_not_vault_fallback():
    coordinator, hello, backend, migration = _coordinator()

    result = coordinator.authorize_for_batch("gemini", 100)

    assert result.status == "backend_result"
    assert result.backend_status == "not_found"
    assert len(hello.request_calls) == 1
    assert backend.read_calls == ["gemini"]
    assert migration.first_calls == 0 and migration.restart_calls == 0


def test_stored_use_consent_does_not_authorize_management_action():
    coordinator, hello, backend, _migration = _coordinator(backend=FakeCredentialBackend({"gemini": TEST_SECRET}))

    assert coordinator.authorize_for_batch("gemini", 100).status == "authorized"
    assert coordinator.delete_persistent_key("gemini", 100).backend_status == "deleted"

    assert len(hello.request_calls) == 2
    assert backend.delete_calls == ["gemini"]


@pytest.mark.parametrize("method_name", ("save_persistent_key", "replace_persistent_key", "delete_persistent_key", "reveal_persistent_key", "authorize_copy_persistent_key"))
def test_every_management_operation_requires_fresh_verification(method_name):
    backend = FakeCredentialBackend({"gemini": TEST_SECRET})
    coordinator, hello, _backend, _migration = _coordinator(backend=backend)
    method = getattr(coordinator, method_name)

    if method_name in {"save_persistent_key", "replace_persistent_key"}:
        first = method("gemini", TEST_SECRET, 100)
        second = method("gemini", TEST_SECRET, 100)
    else:
        first = method("gemini", 100)
        second = method("gemini", 100)

    assert first.status in {"authorized", "backend_result"}
    assert second.status in {"authorized", "backend_result"}
    assert len(hello.check_calls) == 2
    assert len(hello.request_calls) == 2


def test_save_does_not_replace_and_conflict_never_overwrites():
    backend = FakeCredentialBackend({"gemini": OTHER_SECRET})
    coordinator, _hello, _backend, _migration = _coordinator(backend=backend)

    result = coordinator.save_persistent_key("gemini", TEST_SECRET, 100)

    assert result.backend_status == "conflict"
    assert backend.write_calls == [("gemini", False)]
    assert backend.entries["gemini"] == OTHER_SECRET


def test_replace_is_the_only_path_that_allows_explicit_replacement():
    backend = FakeCredentialBackend({"gemini": OTHER_SECRET})
    coordinator, _hello, _backend, _migration = _coordinator(backend=backend)

    result = coordinator.replace_persistent_key("gemini", TEST_SECRET, 100)

    assert result.backend_status == "stored"
    assert backend.write_calls == [("gemini", True)]
    assert backend.entries["gemini"] == TEST_SECRET


@pytest.mark.parametrize("method_name", ("save_persistent_key", "replace_persistent_key", "delete_persistent_key", "reveal_persistent_key", "authorize_copy_persistent_key"))
def test_canceled_management_operation_never_touches_credential_backend(method_name):
    coordinator, hello, backend, _migration = _coordinator(hello=FakeHelloBackend(verifications=["canceled"]), backend=FakeCredentialBackend({"gemini": TEST_SECRET}))
    method = getattr(coordinator, method_name)

    result = method("gemini", TEST_SECRET, 100) if method_name in {"save_persistent_key", "replace_persistent_key"} else method("gemini", 100)

    assert result.status == "consent_not_verified"
    assert result.hello_status == "canceled"
    assert backend.read_calls == [] and backend.write_calls == [] and backend.delete_calls == []
    assert len(hello.request_calls) == 1


def test_persistent_backend_failure_does_not_claim_success_or_expose_secret():
    backend = FakeCredentialBackend()
    backend.write_plan.append(FakeResult("write_failed", error_type="FakeWriteFailure"))
    coordinator, _hello, _backend, _migration = _coordinator(backend=backend)

    result = coordinator.save_persistent_key("gemini", TEST_SECRET, 100)

    assert result.status == "backend_result"
    assert result.backend_status == "write_failed"
    assert TEST_SECRET not in repr(result)


def test_copy_returns_one_shot_lease_without_clipboard_access():
    coordinator, _hello, backend, _migration = _coordinator(backend=FakeCredentialBackend({"gemini": TEST_SECRET}))

    result = coordinator.authorize_copy_persistent_key("gemini", 100)

    assert result.status == "authorized"
    assert result.lease is not None and result.lease._matches_for_testing(TEST_SECRET)
    assert backend.read_calls == ["gemini"]
    assert not hasattr(coordinator, "clipboard")


def test_wrong_thread_rejects_hello_and_management_work():
    coordinator, hello, backend, migration = _coordinator(thread_checker=lambda: False)

    batch = coordinator.authorize_for_batch("gemini", 100)
    save = coordinator.save_persistent_key("gemini", TEST_SECRET, 100)
    begin = coordinator.begin_legacy_migration(100)

    assert batch.status == save.status == begin.status == "wrong_thread"
    assert hello.check_calls == [] and hello.request_calls == []
    assert backend.read_calls == [] and backend.write_calls == []
    assert migration.first_calls == 0


@pytest.mark.parametrize("hwnd", (0, -1, True, "100", None))
def test_invalid_hwnd_never_reaches_hello_or_backend(hwnd):
    coordinator, hello, backend, _migration = _coordinator()

    result = coordinator.authorize_for_batch("gemini", hwnd)

    assert result.status == "invalid_hwnd"
    assert hello.check_calls == [] and hello.request_calls == []
    assert backend.read_calls == []


def test_ui_created_lease_can_be_used_by_worker_without_hello_call():
    coordinator, hello, _backend, _migration = _coordinator()
    coordinator.set_session_key("gemini", TEST_SECRET)
    lease = coordinator.authorize_for_batch("gemini", 100).lease
    results = []

    worker = threading.Thread(target=lambda: results.append(lease is not None and lease._matches_for_testing(TEST_SECRET)))
    worker.start()
    worker.join(timeout=2)

    assert results == [True]
    assert hello.request_calls == []


def test_migration_begin_and_continue_each_require_fresh_consent():
    coordinator, hello, _backend, migration = _coordinator()

    begin = coordinator.begin_legacy_migration(100)
    continued = coordinator.continue_legacy_migration_and_cleanup(100)

    assert begin.migration_phase == "restart_verification_pending"
    assert continued.migration_phase == "legacy_cleanup_complete"
    assert migration.first_calls == 1 and migration.restart_calls == 1
    assert len(hello.request_calls) == 2
    assert coordinator.stored_use_consent_granted is False


def test_canceled_migration_does_not_call_engine_or_cleanup():
    coordinator, hello, _backend, migration = _coordinator(hello=FakeHelloBackend(verifications=["canceled", "canceled"]))

    begin = coordinator.begin_legacy_migration(100)
    continued = coordinator.continue_legacy_migration_and_cleanup(100)

    assert begin.status == continued.status == "consent_not_verified"
    assert migration.first_calls == 0 and migration.restart_calls == 0
    assert len(hello.request_calls) == 2


def test_migration_conflict_and_cleanup_failure_are_reported_without_recovery_side_effects():
    conflict = FakeMigrationEngine(first=FakeResult("ok", phase="conflict"))
    coordinator, _hello, _backend, _migration = _coordinator(migration=conflict)
    conflict_result = coordinator.begin_legacy_migration(100)
    assert conflict_result.migration_phase == "conflict"

    pending = FakeMigrationEngine(restart=FakeResult("ok", phase="legacy_cleanup_pending", retryable=True))
    coordinator, _hello, _backend, _migration = _coordinator(migration=pending)
    pending_result = coordinator.continue_legacy_migration_and_cleanup(100)
    assert pending_result.migration_phase == "legacy_cleanup_pending"
    assert pending_result.retryable is True


def test_events_results_and_exception_boundaries_are_secret_free():
    events = []
    coordinator, _hello, backend, _migration = _coordinator(events=events, backend=FakeCredentialBackend({"gemini": TEST_SECRET}))
    result = coordinator.reveal_persistent_key("gemini", 100)
    backend.read_plan.append(FakeResult("error", error_type="SyntheticFailure"))
    failure = coordinator.authorize_for_batch("openai", 100)

    assert result.lease is not None
    assert TEST_SECRET not in repr(result)
    assert TEST_SECRET not in repr(events)
    assert TEST_SECRET not in repr(failure)


def test_import_and_coordinator_creation_have_no_filesystem_or_native_side_effects(isolated_environment):
    before = {name: sorted(path.name for path in value.iterdir()) for name, value in isolated_environment.items()}
    coordinator, hello, backend, migration = _coordinator()
    after = {name: sorted(path.name for path in value.iterdir()) for name, value in isolated_environment.items()}

    assert coordinator.stored_use_consent_granted is False
    assert hello.check_calls == [] and hello.request_calls == []
    assert backend.read_calls == [] and backend.write_calls == [] and backend.delete_calls == []
    assert migration.first_calls == 0 and migration.restart_calls == 0
    assert after == before


def test_provider_validation_does_not_embed_secret_in_result_or_event():
    events = []
    coordinator, _hello, _backend, _migration = _coordinator(events=events)

    result = coordinator.save_persistent_key("ollama", TEST_SECRET, 100)

    assert result.status == "invalid_input"
    assert TEST_SECRET not in repr(result)
    assert TEST_SECRET not in repr(events)


def test_resetting_process_consent_requires_a_new_stored_use_verification():
    coordinator, hello, _backend, _migration = _coordinator(backend=FakeCredentialBackend({"gemini": TEST_SECRET}))

    assert coordinator.authorize_for_batch("gemini", 100).status == "authorized"
    coordinator.reset_stored_use_consent()
    assert coordinator.authorize_for_batch("gemini", 100).status == "authorized"

    assert len(hello.request_calls) == 2


def test_management_hello_unavailable_never_touches_persistent_storage():
    coordinator, hello, backend, _migration = _coordinator(
        hello=FakeHelloBackend(availability="not_configured_for_user"),
        backend=FakeCredentialBackend({"gemini": OTHER_SECRET}),
    )

    result = coordinator.save_persistent_key("gemini", TEST_SECRET, 100)

    assert result.status == "hello_unavailable"
    assert result.persistent_operations_allowed is False
    assert result.session_only_allowed is True
    assert hello.request_calls == []
    assert backend.write_calls == []
    assert backend.entries["gemini"] == OTHER_SECRET


def test_management_consent_does_not_establish_ordinary_stored_use_consent():
    coordinator, hello, _backend, _migration = _coordinator(backend=FakeCredentialBackend({"gemini": TEST_SECRET}))

    assert coordinator.save_persistent_key("gemini", TEST_SECRET, 100).backend_status == "already_present"
    assert coordinator.stored_use_consent_granted is False
    assert coordinator.authorize_for_batch("gemini", 100).status == "authorized"

    assert len(hello.request_calls) == 2


def test_replace_backend_failure_leaves_existing_fake_entry_unchanged():
    backend = FakeCredentialBackend({"gemini": OTHER_SECRET})
    backend.write_plan.append(FakeResult("verification_failed", error_type="FakeVerificationFailure"))
    coordinator, _hello, _backend, _migration = _coordinator(backend=backend)

    result = coordinator.replace_persistent_key("gemini", TEST_SECRET, 100)

    assert result.backend_status == "verification_failed"
    assert backend.entries["gemini"] == OTHER_SECRET


def test_delete_only_targets_requested_provider():
    backend = FakeCredentialBackend({"gemini": TEST_SECRET, "openai": OTHER_SECRET})
    coordinator, _hello, _backend, _migration = _coordinator(backend=backend)

    result = coordinator.delete_persistent_key("gemini", 100)

    assert result.backend_status == "deleted"
    assert "gemini" not in backend.entries
    assert "openai" in backend.entries
    assert backend.delete_calls == ["gemini"]


def test_reveal_and_copy_each_return_only_one_short_lived_lease():
    coordinator, hello, _backend, _migration = _coordinator(backend=FakeCredentialBackend({"gemini": TEST_SECRET}))

    reveal = coordinator.reveal_persistent_key("gemini", 100)
    copy = coordinator.authorize_copy_persistent_key("gemini", 100)

    assert reveal.lease is not None and reveal.lease.active
    assert copy.lease is not None and copy.lease.active
    reveal.lease.close()
    copy.lease.close()
    assert not reveal.lease.active and not copy.lease.active
    assert len(hello.request_calls) == 2


@pytest.mark.parametrize("method_name", ("begin_legacy_migration", "continue_legacy_migration_and_cleanup"))
def test_unavailable_hello_never_starts_migration(method_name):
    coordinator, hello, _backend, migration = _coordinator(hello=FakeHelloBackend(availability="disabled_by_policy"))

    result = getattr(coordinator, method_name)(100)

    assert result.status == "hello_unavailable"
    assert hello.request_calls == []
    assert migration.first_calls == 0 and migration.restart_calls == 0


@pytest.mark.parametrize("method_name", ("begin_legacy_migration", "continue_legacy_migration_and_cleanup"))
def test_migration_engine_exception_is_retryable_and_secret_free(method_name):
    class RaisingMigration:
        def first_run(self):
            raise RuntimeError(TEST_SECRET)

        def restart_verify_and_cleanup(self):
            raise RuntimeError(TEST_SECRET)

    coordinator, _hello, _backend, _migration = _coordinator(migration=RaisingMigration())
    result = getattr(coordinator, method_name)(100)

    assert result.status == "migration_error"
    assert result.retryable is True
    assert TEST_SECRET not in repr(result)


def test_backend_exception_is_sanitized_and_never_uses_migration_as_fallback():
    class RaisingBackend(FakeCredentialBackend):
        def read(self, provider_id):
            raise RuntimeError(TEST_SECRET)

    migration = FakeMigrationEngine()
    coordinator, _hello, _backend, _migration = _coordinator(backend=RaisingBackend(), migration=migration)

    result = coordinator.authorize_for_batch("gemini", 100)

    assert result.status == "backend_result"
    assert result.backend_status == "error"
    assert TEST_SECRET not in repr(result)
    assert migration.first_calls == 0 and migration.restart_calls == 0


def test_event_contains_only_approved_nonsecret_fields():
    events = []
    coordinator, _hello, _backend, _migration = _coordinator(events=events)

    result = coordinator.set_session_key("gemini", TEST_SECRET)

    assert result.status == "session_key_set"
    assert len(events) == 1
    event = events[0]
    assert event.provider_id == "gemini"
    assert TEST_SECRET not in repr(event)
    assert not hasattr(event, "secret")


def test_module_has_no_legacy_vault_or_native_backend_construction_path():
    source = open(policy.__file__, "r", encoding="utf-8").read()

    assert "load_vault(" not in source
    assert "keys.vault" not in source
    assert "WindowsHelloConsentBackend(" not in source
    assert "WindowsCredentialManagerBackend(" not in source
