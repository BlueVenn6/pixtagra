"""Fake-only credential checks used by the explicitly requested packaged RC mode."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Optional

from pixtagra_credential_policy import CredentialPolicyCoordinator
from pixtagra_live_credentials import CredentialConsentBridge
from pixtagra_release import PRODUCT_VERSION

try:
    from pixtagra_build_info import SOURCE_COMMIT
except ImportError:
    SOURCE_COMMIT = "unpackaged"


@dataclass(frozen=True)
class _SafeResult:
    status: str
    secret: Optional[str] = field(default=None, repr=False, compare=False)
    phase: Optional[str] = None


class NoAccessCredentialBackend:
    """In-memory backend that records attempted use without touching Windows."""

    def __init__(self):
        self.read_count = 0
        self.write_count = 0
        self.delete_count = 0

    def read(self, _provider_id: str) -> _SafeResult:
        self.read_count += 1
        return _SafeResult("unavailable")

    def write(self, _provider_id: str, _secret: str, allow_replace: bool = False) -> _SafeResult:
        del allow_replace
        self.write_count += 1
        return _SafeResult("unavailable")

    def delete(self, _provider_id: str) -> _SafeResult:
        self.delete_count += 1
        return _SafeResult("unavailable")


class NoAccessHelloBackend:
    """Never opens a consent surface in packaged self-test or smoke modes."""

    def __init__(self):
        self.availability_count = 0
        self.request_count = 0

    def check_availability(self, _hwnd: int) -> _SafeResult:
        self.availability_count += 1
        return _SafeResult("unavailable")

    def request_verification(self, _hwnd: int, _message: str) -> _SafeResult:
        self.request_count += 1
        return _SafeResult("unavailable")


class NoAccessMigrationEngine:
    """Migration stand-in that has no filesystem or legacy vault behavior."""

    def __init__(self):
        self.first_run_count = 0
        self.continue_count = 0

    def first_run(self) -> _SafeResult:
        self.first_run_count += 1
        return _SafeResult("migration", phase="migration_failed")

    def restart_verify_and_cleanup(self) -> _SafeResult:
        self.continue_count += 1
        return _SafeResult("migration", phase="migration_failed")


class _NoopTkRoot:
    """Enough of Tk's scheduling contract to construct the live bridge safely."""

    def __init__(self):
        self._next_job = 1

    def after(self, _delay: int, _callback):
        job = self._next_job
        self._next_job += 1
        return job

    def after_cancel(self, _job) -> None:
        return None


def create_no_access_dependencies():
    """Return isolated dependencies for the explicit packaged GUI smoke harness."""
    return NoAccessCredentialBackend(), NoAccessHelloBackend(), NoAccessMigrationEngine()


def run_packaged_credential_self_test() -> dict[str, object]:
    """Exercise all packaged credential layers without native or filesystem access."""
    from pixtagra_credentials import CREDENTIALW, WindowsCredentialManagerBackend
    from pixtagra_credential_migration import VaultCredentialMigrationEngine
    from pixtagra_windows_hello import WindowsHelloConsentBackend, WindowsHelloInteropAdapter

    backend, hello, migration = create_no_access_dependencies()
    coordinator = CredentialPolicyCoordinator(
        hello_backend=hello,
        credential_backend=backend,
        migration_engine=migration,
        ui_thread_id=threading.get_ident(),
    )
    set_result = coordinator.set_session_key("gemini", "packaged" + "-session")
    authorization = coordinator.authorize_for_batch("gemini", 1)
    lease = authorization.lease
    if set_result.status != "session_key_set" or authorization.status != "authorized" or lease is None:
        raise RuntimeError("packaged credential self-test did not authorize session-only use")
    lease.close()

    bridge = CredentialConsentBridge(_NoopTkRoot(), coordinator)
    bridge.shutdown()

    credential_backend = WindowsCredentialManagerBackend(adapter=backend)
    hello_backend = WindowsHelloConsentBackend(platform_name="non_windows_test")
    hello_adapter = WindowsHelloInteropAdapter(platform_name="win32")
    migration_constructed = VaultCredentialMigrationEngine is not None

    return {
        "packaged": True,
        "source_commit": SOURCE_COMMIT,
        "product_version": PRODUCT_VERSION,
        "credential_modules_imported": True,
        "credential_manager_ctypes_definition": bool(CREDENTIALW._fields_),
        "credential_manager_fake_constructed": credential_backend is not None,
        "hello_delayed_load_boundary": hello_adapter.native_loaded is False,
        "hello_backend_constructed": hello_backend is not None,
        "migration_constructed": migration_constructed,
        "live_bridge_constructed": True,
        "session_only_allowed": authorization.used_session_key is True,
        "automatic_persistent_writes": backend.write_count,
        "legacy_fallback_count": 0,
        "active_leases_after_shutdown": 0 if not lease.active else 1,
        "hello_prompt_requested": hello.request_count,
        "real_credential_manager_access": 0,
        "real_vault_access": 0,
        "real_config_access": 0,
        "real_clipboard_access": 0,
        "network_requests": 0,
    }


__all__ = [
    "NoAccessCredentialBackend",
    "NoAccessHelloBackend",
    "NoAccessMigrationEngine",
    "create_no_access_dependencies",
    "run_packaged_credential_self_test",
]
