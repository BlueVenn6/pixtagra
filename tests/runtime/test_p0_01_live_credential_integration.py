"""Fake-only contract tests for live PixTagra credential integration."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time

import pixtagra_credential_policy as policy
from pixtagra_live_credentials import CredentialConsentBridge


TEST_SECRET = "TEST_KEY_DO_NOT_USE"
OTHER_SECRET = "OTHER_TEST_KEY_DO_NOT_USE"


@dataclass(frozen=True)
class FakeResult:
    status: str
    secret: str | None = field(default=None, repr=False, compare=False)
    phase: str | None = None
    error_type: str | None = None
    retryable: bool = True


class FakeHello:
    def __init__(self, verification="verified", availability="available"):
        self.verification = verification
        self.availability = availability
        self.availability_calls = 0
        self.request_calls = 0
        self.worker_ids = []

    def check_availability(self, _hwnd):
        self.availability_calls += 1
        return FakeResult(self.availability)

    def request_verification(self, _hwnd, _message):
        self.request_calls += 1
        self.worker_ids.append(threading.get_ident())
        return FakeResult(self.verification)


class FakeCredentialManager:
    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.read_calls = []
        self.write_calls = []
        self.delete_calls = []

    def read(self, provider):
        self.read_calls.append(provider)
        if provider not in self.entries:
            return FakeResult("not_found")
        return FakeResult("found", secret=self.entries[provider])

    def write(self, provider, secret, allow_replace=False):
        self.write_calls.append((provider, bool(allow_replace)))
        if provider in self.entries and self.entries[provider] != secret and not allow_replace:
            return FakeResult("conflict")
        self.entries[provider] = secret
        return FakeResult("stored")

    def delete(self, provider):
        self.delete_calls.append(provider)
        self.entries.pop(provider, None)
        return FakeResult("deleted")


class FakeMigration:
    def __init__(self):
        self.first_calls = 0
        self.continue_calls = 0

    def first_run(self):
        self.first_calls += 1
        return FakeResult("migration", phase="restart_verification_pending")

    def restart_verify_and_cleanup(self):
        self.continue_calls += 1
        return FakeResult("migration", phase="legacy_cleanup_complete")


class FakeRoot:
    def __init__(self):
        self.callbacks = []
        self.after_threads = []
        self.canceled_jobs = []

    def after(self, _delay, callback):
        self.after_threads.append(threading.get_ident())
        self.callbacks.append(callback)
        return len(self.callbacks)

    def after_cancel(self, job):
        self.canceled_jobs.append(job)

    def drain(self, predicate, limit=100):
        for _ in range(limit):
            if predicate():
                return True
            if self.callbacks:
                self.callbacks.pop(0)()
                time.sleep(0.005)
            else:
                time.sleep(0.01)
        return predicate()


def _stack(*, verification="verified", availability="available", entries=None):
    hello = FakeHello(verification, availability)
    backend = FakeCredentialManager(entries)
    migration = FakeMigration()
    coordinator = policy.CredentialPolicyCoordinator(
        hello_backend=hello,
        credential_backend=backend,
        migration_engine=migration,
    )
    root = FakeRoot()
    bridge = CredentialConsentBridge(root, coordinator)
    return root, bridge, coordinator, hello, backend, migration


def _start_and_wait(root, bridge, operation, provider, *, secret=None):
    results = []
    bridge.start(
        operation, provider, 101,
        secret_supplier=(lambda: secret) if secret is not None else None,
        on_complete=results.append,
    )
    assert root.drain(lambda: bool(results))
    return results[0]


def test_live_credential_bridge_contract_is_available():
    assert CredentialConsentBridge is not None


def test_session_only_batch_never_uses_hello_or_credential_manager():
    root, bridge, coordinator, hello, backend, _migration = _stack(entries={"gemini": OTHER_SECRET})
    assert coordinator.set_session_key("gemini", TEST_SECRET).status == "session_key_set"

    result = _start_and_wait(root, bridge, "batch", "gemini")

    assert result.status == "authorized" and result.used_session_key
    assert hello.request_calls == 0
    assert backend.read_calls == [] and backend.write_calls == []
    assert result.lease is not None and result.lease._matches_for_testing(TEST_SECRET)


def test_stored_batch_uses_one_worker_verification_then_process_consent():
    root, bridge, _coordinator, hello, backend, _migration = _stack(entries={"gemini": TEST_SECRET})
    first = _start_and_wait(root, bridge, "batch", "gemini")
    second = _start_and_wait(root, bridge, "batch", "gemini")

    assert first.status == second.status == "authorized"
    assert hello.request_calls == 1
    assert hello.worker_ids and hello.worker_ids[0] != threading.get_ident()
    assert backend.read_calls == ["gemini", "gemini"]
    assert all(thread_id == threading.get_ident() for thread_id in root.after_threads)


def test_canceled_batch_never_reads_or_submits_a_lease():
    root, bridge, _coordinator, hello, backend, _migration = _stack(verification="canceled", entries={"gemini": TEST_SECRET})
    result = _start_and_wait(root, bridge, "batch", "gemini")

    assert result.status == "consent_not_verified"
    assert result.hello_status == "canceled"
    assert hello.request_calls == 1 and backend.read_calls == []
    assert result.lease is None


def test_unavailable_hello_allows_only_session_key():
    root, bridge, coordinator, hello, backend, _migration = _stack(availability="not_configured_for_user", entries={"gemini": TEST_SECRET})
    denied = _start_and_wait(root, bridge, "batch", "gemini")
    coordinator.set_session_key("gemini", TEST_SECRET)
    allowed = _start_and_wait(root, bridge, "batch", "gemini")

    assert denied.status == "hello_unavailable"
    assert denied.persistent_operations_allowed is False and denied.session_only_allowed is True
    assert allowed.status == "authorized" and allowed.used_session_key
    assert hello.request_calls == 0 and backend.read_calls == []


def test_save_replace_and_delete_each_require_fresh_consent():
    root, bridge, _coordinator, hello, backend, _migration = _stack(entries={"gemini": OTHER_SECRET})
    save = _start_and_wait(root, bridge, "save", "gemini", secret=TEST_SECRET)
    replace = _start_and_wait(root, bridge, "replace", "gemini", secret=TEST_SECRET)
    delete = _start_and_wait(root, bridge, "delete", "gemini")

    assert save.backend_status == "conflict"
    assert replace.backend_status == "stored"
    assert delete.backend_status == "deleted"
    assert hello.request_calls == 3
    assert backend.write_calls == [("gemini", False), ("gemini", True)]
    assert backend.delete_calls == ["gemini"]


def test_management_consent_never_grants_ordinary_stored_use():
    root, bridge, coordinator, hello, backend, _migration = _stack(entries={"gemini": TEST_SECRET})
    assert _start_and_wait(root, bridge, "delete", "gemini").backend_status == "deleted"
    backend.entries["gemini"] = TEST_SECRET
    assert _start_and_wait(root, bridge, "batch", "gemini").status == "authorized"

    assert coordinator.stored_use_consent_granted is True
    assert hello.request_calls == 2


def test_migration_begin_and_continue_need_distinct_consent():
    root, bridge, _coordinator, hello, _backend, migration = _stack()
    begin = _start_and_wait(root, bridge, "migration_begin", None)
    complete = _start_and_wait(root, bridge, "migration_continue", None)

    assert begin.migration_phase == "restart_verification_pending"
    assert complete.migration_phase == "legacy_cleanup_complete"
    assert migration.first_calls == migration.continue_calls == 1
    assert hello.request_calls == 2


def test_bridge_serializes_operations_and_shutdown_cancels_without_storage():
    root, bridge, _coordinator, _hello, backend, _migration = _stack(entries={"gemini": TEST_SECRET})
    pending = bridge.start("batch", "gemini", 101, on_complete=lambda _result: None)
    busy = bridge.start("save", "gemini", 101, secret_supplier=lambda: TEST_SECRET, on_complete=lambda _result: None)
    bridge.shutdown()

    assert pending.status == "verification_required"
    assert busy.status == "consent_busy"
    assert backend.write_calls == []
    assert root.canceled_jobs


def test_leases_are_invalidated_after_batch_completion_path():
    root, bridge, _coordinator, _hello, _backend, _migration = _stack()
    _coordinator.set_session_key("gemini", TEST_SECRET)
    result = _start_and_wait(root, bridge, "batch", "gemini")
    lease = result.lease
    assert lease is not None and lease.active
    lease.close()
    assert lease.secret_for_authorized_use() is None


def test_live_application_source_uses_policy_and_never_calls_credui_or_daily_vault_fallback():
    source = open("pixtagra.py", "r", encoding="utf-8").read()

    assert "CredentialPolicyCoordinator" in source
    assert "CredentialConsentBridge" in source
    assert "CredUI" not in source
    assert "load_vault().get(provider" not in source
    assert "Auto-save working key to vault" not in source
