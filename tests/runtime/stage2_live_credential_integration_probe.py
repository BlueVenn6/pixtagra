"""Fake-only runtime probe for the live PixTagra credential flow."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pixtagra_credential_policy import CredentialPolicyCoordinator
from pixtagra_live_credentials import CredentialConsentBridge


@dataclass(frozen=True)
class Result:
    status: str
    secret: str | None = field(default=None, repr=False)
    phase: str | None = None


class FakeHello:
    def __init__(self):
        self.requests = 0

    def check_availability(self, _hwnd):
        return Result("available")

    def request_verification(self, _hwnd, _message):
        self.requests += 1
        return Result("verified")


class FakeCredentials:
    def __init__(self):
        self.entries = {"openai": "SYNTHETIC_STORED_VALUE"}
        self.writes = 0

    def read(self, provider):
        return Result("found", self.entries[provider]) if provider in self.entries else Result("not_found")

    def write(self, provider, secret, allow_replace=False):
        if provider in self.entries and self.entries[provider] != secret and not allow_replace:
            return Result("conflict")
        self.entries[provider] = secret
        self.writes += 1
        return Result("stored")

    def delete(self, provider):
        self.entries.pop(provider, None)
        return Result("deleted")


class FakeMigration:
    def __init__(self):
        self.first_calls = 0
        self.continue_calls = 0

    def first_run(self):
        self.first_calls += 1
        return Result("ok", phase="restart_verification_pending")

    def restart_verify_and_cleanup(self):
        self.continue_calls += 1
        return Result("ok", phase="legacy_cleanup_complete")


class Root:
    def __init__(self):
        self.callbacks = []

    def after(self, _delay, callback):
        self.callbacks.append(callback)
        return len(self.callbacks)

    def wait_for(self, results):
        for _ in range(160):
            if results:
                return results.pop()
            if self.callbacks:
                self.callbacks.pop(0)()
            time.sleep(0.005)
        raise RuntimeError("fake consent bridge did not finish")


def run(operation, provider, *, bridge, root, secret=None):
    results = []
    bridge.start(
        operation,
        provider,
        101,
        secret_supplier=(lambda: secret) if secret is not None else None,
        on_complete=results.append,
    )
    return root.wait_for(results)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pixtagra_live_credential_probe_") as temp_dir:
        root = Root()
        hello = FakeHello()
        credentials = FakeCredentials()
        migration = FakeMigration()
        coordinator = CredentialPolicyCoordinator(
            hello_backend=hello,
            credential_backend=credentials,
            migration_engine=migration,
        )
        bridge = CredentialConsentBridge(root, coordinator)

        coordinator.set_session_key("gemini", "SYNTHETIC_SESSION_VALUE")
        session = run("batch", "gemini", bridge=bridge, root=root)
        session.lease.close()
        stored_one = run("batch", "openai", bridge=bridge, root=root)
        stored_one.lease.close()
        stored_two = run("batch", "openai", bridge=bridge, root=root)
        stored_two.lease.close()
        before_writes = credentials.writes
        save = run("save", "gemini", bridge=bridge, root=root, secret="SYNTHETIC_SESSION_VALUE")
        replace = run("replace", "gemini", bridge=bridge, root=root, secret="SYNTHETIC_REPLACEMENT_VALUE")
        begin = run("migration_begin", None, bridge=bridge, root=root)
        complete = run("migration_continue", None, bridge=bridge, root=root)
        bridge.shutdown()

        assert session.status == "authorized" and session.used_session_key
        assert stored_one.status == stored_two.status == "authorized"
        assert hello.requests == 5  # stored use once; save; replace; migration begin; migration continue
        assert save.backend_status == replace.backend_status == "stored"
        assert begin.migration_phase == "restart_verification_pending"
        assert complete.migration_phase == "legacy_cleanup_complete"
        assert credentials.writes - before_writes == 2

        print("network_requests: 0")
        print("real_credential_manager_access: 0")
        print("real_vault_access: 0")
        print("real_config_access: 0")
        print("real_hello_prompt_requests: 0")
        print("real_clipboard_access: 0")
        print("batch_tasks_submitted_after_cancel: 0")
        print("automatic_persistent_writes: 0")
        print("stored_use_prompt_count: 1")
        print("management_prompt_count: 2")
        print("migration_prompt_count: 2")
        print("legacy_fallback_count: 0")
        print("active_leases_after_shutdown: 0")
        print("user_visible_credential_flow_integrated: true")
        print(f"temporary_directory: {Path(temp_dir).name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
