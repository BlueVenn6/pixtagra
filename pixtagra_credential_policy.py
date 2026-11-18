"""Injected credential-consent policy for a future PixTagra integration.

This module deliberately does not create native backends or access the active
Vault.  It only coordinates dependencies supplied by a future UI integration.
Windows Hello is application-level user consent, not a cryptographic key.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import threading
from typing import Any, Callable, Optional, Protocol


APPROVED_PROVIDER_IDS = ("gemini", "openai", "kimi", "qwen", "minimax", "zhipu")
CREDENTIAL_BLOB_MAX_BYTES = 2560
_MAX_HWND = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1


class HelloConsentBackend(Protocol):
    def check_availability(self, hwnd: int) -> Any:
        ...

    def request_verification(self, hwnd: int, message: str) -> Any:
        ...


class CredentialManagerBackend(Protocol):
    def read(self, provider_id: str) -> Any:
        ...

    def write(self, provider_id: str, secret: str, allow_replace: bool = False) -> Any:
        ...

    def delete(self, provider_id: str) -> Any:
        ...


class LegacyMigrationEngine(Protocol):
    def first_run(self) -> Any:
        ...

    def restart_verify_and_cleanup(self) -> Any:
        ...


@dataclass(frozen=True)
class CredentialPolicyResult:
    """A redacted operation result. Secret-bearing leases are never repr'd."""

    operation: str
    status: str
    provider_id: Optional[str] = None
    hello_status: Optional[str] = None
    backend_status: Optional[str] = None
    migration_phase: Optional[str] = None
    persistent_operations_allowed: bool = False
    session_only_allowed: bool = True
    used_session_key: bool = False
    retryable: bool = False
    error_type: Optional[str] = None
    lease: Optional["CredentialUseLease"] = field(default=None, repr=False, compare=False)
    challenge: Optional["CredentialConsentChallenge"] = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class CredentialConsentChallenge:
    """One non-secret consent request owned by the UI coordinator."""

    challenge_id: int
    operation: str
    provider_id: Optional[str]
    hwnd: int = field(repr=False, compare=False)


@dataclass(frozen=True)
class CredentialPolicyEvent:
    """Safe event content for an optional caller-provided audit sink."""

    operation: str
    provider_id: Optional[str]
    status: str
    hello_status: Optional[str]
    backend_status: Optional[str]
    session_only: bool
    retryable: bool
    error_type: Optional[str]


class CredentialUseLease:
    """In-memory secret handoff that refuses serialization and has explicit lifetime."""

    __slots__ = ("_provider_id", "_secret", "_one_shot", "_closed", "_lock")

    def __init__(self, provider_id: str, secret: str, *, one_shot: bool = False):
        self._provider_id = provider_id
        self._secret: Optional[str] = secret
        self._one_shot = bool(one_shot)
        self._closed = False
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"<CredentialUseLease provider={self._provider_id!r} active={not self._closed} one_shot={self._one_shot}>"

    __str__ = __repr__

    def __getstate__(self):
        raise TypeError("CredentialUseLease cannot be serialized")

    def __reduce_ex__(self, protocol):
        raise TypeError("CredentialUseLease cannot be serialized")

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def active(self) -> bool:
        with self._lock:
            return not self._closed and self._secret is not None

    def secret_for_authorized_use(self) -> Optional[str]:
        """Return the live secret only to the caller already granted this lease."""
        with self._lock:
            if self._closed or self._secret is None:
                return None
            secret = self._secret
            if self._one_shot:
                self._secret = None
                self._closed = True
            return secret

    def _matches_for_testing(self, expected: object) -> bool:
        """Test-only comparison helper so failing assertions never print a secret."""
        with self._lock:
            return not self._closed and self._secret == expected

    def close(self) -> None:
        with self._lock:
            self._secret = None
            self._closed = True

    invalidate = close

    def __enter__(self) -> "CredentialUseLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False


class SessionCredentialStore:
    """Thread-safe, process-only secret store with no persistence side effects."""

    def __init__(self):
        self._keys: dict[str, str] = {}
        self._lock = threading.RLock()

    def set(self, provider_id: str, secret: str) -> bool:
        if not _valid_provider(provider_id) or not _valid_secret(secret):
            return False
        with self._lock:
            self._keys[provider_id] = secret
        return True

    def has(self, provider_id: object) -> bool:
        if not _valid_provider(provider_id):
            return False
        with self._lock:
            return provider_id in self._keys

    def lease(self, provider_id: str) -> Optional[CredentialUseLease]:
        with self._lock:
            secret = self._keys.get(provider_id)
        return CredentialUseLease(provider_id, secret) if secret is not None else None

    def clear(self, provider_id: object) -> bool:
        if not _valid_provider(provider_id):
            return False
        with self._lock:
            self._keys.pop(provider_id, None)
        return True

    def clear_all(self) -> None:
        with self._lock:
            self._keys.clear()


def _valid_provider(provider_id: object) -> bool:
    return isinstance(provider_id, str) and provider_id in APPROVED_PROVIDER_IDS


def _valid_secret(secret: object) -> bool:
    if not isinstance(secret, str) or not secret or "\x00" in secret:
        return False
    try:
        return len(secret.encode("utf-8", errors="strict")) <= CREDENTIAL_BLOB_MAX_BYTES
    except UnicodeEncodeError:
        return False


def _valid_hwnd(hwnd: object) -> bool:
    return isinstance(hwnd, int) and not isinstance(hwnd, bool) and 0 < hwnd <= _MAX_HWND


class CredentialPolicyCoordinator:
    """Coordinates future credential UI operations without activating any pathway.

    A verified ordinary stored-use consent lives only in this Python process.
    Management actions intentionally never reuse that flag and always ask the
    injected Hello backend for a fresh verification.
    """

    def __init__(
        self,
        *,
        hello_backend: HelloConsentBackend,
        credential_backend: CredentialManagerBackend,
        migration_engine: LegacyMigrationEngine,
        ui_thread_id: Optional[int] = None,
        thread_checker: Optional[Callable[[], bool]] = None,
        event_sink: Optional[Callable[[CredentialPolicyEvent], None]] = None,
        session_store: Optional[SessionCredentialStore] = None,
    ):
        self._hello_backend = hello_backend
        self._credential_backend = credential_backend
        self._migration_engine = migration_engine
        self._ui_thread_id = threading.get_ident() if ui_thread_id is None else ui_thread_id
        self._thread_checker = thread_checker
        self._event_sink = event_sink
        self._session_store = session_store or SessionCredentialStore()
        self._stored_use_consent = False
        self._lock = threading.RLock()
        self._active_challenge: Optional[CredentialConsentChallenge] = None
        self._next_challenge_id = 1

    @property
    def stored_use_consent_granted(self) -> bool:
        with self._lock:
            return self._stored_use_consent

    def reset_stored_use_consent(self) -> None:
        with self._lock:
            self._stored_use_consent = False

    def set_session_key(self, provider_id: object, secret: object) -> CredentialPolicyResult:
        if not _valid_provider(provider_id) or not _valid_secret(secret):
            return self._emit(self._result("set_session_key", "invalid_input", None))
        stored = self._session_store.set(provider_id, secret)
        return self._emit(self._result("set_session_key", "session_key_set" if stored else "invalid_input", provider_id, used_session_key=stored))

    def clear_session_key(self, provider_id: object) -> CredentialPolicyResult:
        if not _valid_provider(provider_id):
            return self._emit(self._result("clear_session_key", "invalid_input", None))
        self._session_store.clear(provider_id)
        return self._emit(self._result("clear_session_key", "session_key_cleared", provider_id, used_session_key=True))

    def clear_all_session_keys(self) -> CredentialPolicyResult:
        self._session_store.clear_all()
        return self._emit(self._result("clear_all_session_keys", "session_keys_cleared"))

    def has_session_key(self, provider_id: object) -> bool:
        return self._session_store.has(provider_id)

    def authorize_for_batch(self, provider_id: object, hwnd: object) -> CredentialPolicyResult:
        operation = "authorize_for_batch"
        if not _valid_provider(provider_id):
            return self._emit(self._result(operation, "invalid_input", None))
        thread_error = self._require_ui_thread(operation, provider_id)
        if thread_error:
            return thread_error
        hwnd_error = self._require_hwnd(operation, provider_id, hwnd)
        if hwnd_error:
            return hwnd_error

        session_lease = self._session_store.lease(provider_id)
        if session_lease is not None:
            return self._emit(self._result(operation, "authorized", provider_id, used_session_key=True, lease=session_lease))

        with self._lock:
            consent_granted = self._stored_use_consent
        if not consent_granted:
            consent = self._fresh_consent(operation, provider_id, hwnd)
            if consent is not None:
                return consent
            with self._lock:
                self._stored_use_consent = True

        return self._read_persistent_lease(operation, provider_id)

    def begin_interactive_operation(self, operation: str, provider_id: object, hwnd: object) -> CredentialPolicyResult:
        """Start a UI-owned operation without synchronously showing Windows Hello.

        The caller runs :meth:`request_interactive_verification` in a dedicated
        consent worker, then returns to the UI thread for
        :meth:`complete_interactive_operation`.  No backend or migration action
        is performed until a fresh verified result reaches this method.
        """
        operation_map = {
            "batch": "authorize_for_batch",
            "save": "save_persistent_key",
            "replace": "replace_persistent_key",
            "delete": "delete_persistent_key",
            "reveal": "reveal_persistent_key",
            "copy": "authorize_copy_persistent_key",
            "migration_begin": "begin_legacy_migration",
            "migration_continue": "continue_legacy_migration_and_cleanup",
        }
        public_operation = operation_map.get(operation)
        if public_operation is None:
            return self._emit(self._result("interactive_operation", "invalid_input"))
        required_provider = operation not in {"migration_begin", "migration_continue"}
        provider = provider_id if _valid_provider(provider_id) else None
        if required_provider and provider is None:
            return self._emit(self._result(public_operation, "invalid_input"))
        thread_error = self._require_ui_thread(public_operation, provider)
        if thread_error:
            return thread_error
        hwnd_error = self._require_hwnd(public_operation, provider, hwnd)
        if hwnd_error:
            return hwnd_error

        if operation == "batch":
            session_lease = self._session_store.lease(provider)
            if session_lease is not None:
                return self._emit(self._result(public_operation, "authorized", provider, used_session_key=True, lease=session_lease))
            with self._lock:
                consent_granted = self._stored_use_consent
            if consent_granted:
                return self._read_persistent_lease(public_operation, provider)

        availability = self._safe_hello_call("check_availability", hwnd)
        availability_status = _result_status(availability)
        if availability_status != "available":
            return self._emit(self._result(public_operation, "hello_unavailable", provider, hello_status=availability_status, error_type=_safe_error_type(availability)))

        with self._lock:
            if self._active_challenge is not None:
                return self._emit(self._result(public_operation, "consent_busy", provider, retryable=True))
            challenge = CredentialConsentChallenge(
                challenge_id=self._next_challenge_id,
                operation=operation,
                provider_id=provider,
                hwnd=hwnd,
            )
            self._next_challenge_id += 1
            self._active_challenge = challenge
        return self._emit(self._result(public_operation, "verification_required", provider, challenge=challenge))

    def request_interactive_verification(self, challenge: CredentialConsentChallenge) -> Any:
        """Run only from the consent worker; it cannot access secret storage."""
        with self._lock:
            active = self._active_challenge
        if active != challenge:
            return _FailureResult("error", "InvalidConsentChallenge")
        return self._safe_hello_call(
            "request_verification",
            challenge.hwnd,
            "PixTagra is verifying your identity for a credential security action.",
        )

    def cancel_interactive_operation(self, challenge: CredentialConsentChallenge) -> None:
        with self._lock:
            if self._active_challenge == challenge:
                self._active_challenge = None

    def complete_interactive_operation(
        self,
        challenge: CredentialConsentChallenge,
        verification: object,
        secret: object = None,
    ) -> CredentialPolicyResult:
        """Consume a verified challenge on the UI thread and perform one action."""
        provider = challenge.provider_id if _valid_provider(challenge.provider_id) else None
        operation_map = {
            "batch": "authorize_for_batch",
            "save": "save_persistent_key",
            "replace": "replace_persistent_key",
            "delete": "delete_persistent_key",
            "reveal": "reveal_persistent_key",
            "copy": "authorize_copy_persistent_key",
            "migration_begin": "begin_legacy_migration",
            "migration_continue": "continue_legacy_migration_and_cleanup",
        }
        public_operation = operation_map.get(challenge.operation, "interactive_operation")
        thread_error = self._require_ui_thread(public_operation, provider)
        if thread_error:
            return thread_error
        with self._lock:
            if self._active_challenge != challenge:
                return self._emit(self._result(public_operation, "invalid_challenge", provider))
            self._active_challenge = None
        verification_status = _result_status(verification)
        if verification_status != "verified":
            return self._emit(self._result(public_operation, "consent_not_verified", provider, hello_status=verification_status, error_type=_safe_error_type(verification)))

        if challenge.operation == "batch":
            with self._lock:
                self._stored_use_consent = True
            return self._read_persistent_lease(public_operation, provider)
        if challenge.operation in {"save", "replace"}:
            if not _valid_secret(secret):
                return self._emit(self._result(public_operation, "invalid_input", provider))
            backend = self._safe_call("write", provider, secret, allow_replace=challenge.operation == "replace")
            return self._emit(self._backend_result(public_operation, provider, backend))
        if challenge.operation == "delete":
            return self._emit(self._backend_result(public_operation, provider, self._safe_call("delete", provider)))
        if challenge.operation in {"reveal", "copy"}:
            backend = self._safe_call("read", provider)
            if _result_status(backend) != "found":
                return self._emit(self._backend_result(public_operation, provider, backend))
            found_secret = getattr(backend, "secret", None)
            if not _valid_secret(found_secret):
                return self._emit(self._result(public_operation, "backend_invalid_data", provider, backend_status="invalid_data", error_type="InvalidBackendSecret"))
            return self._emit(self._result(public_operation, "authorized", provider, backend_status="found", lease=CredentialUseLease(provider, found_secret, one_shot=True)))
        method_name = "first_run" if challenge.operation == "migration_begin" else "restart_verify_and_cleanup"
        try:
            migration = getattr(self._migration_engine, method_name)()
        except Exception as exc:
            return self._emit(self._result(public_operation, "migration_error", error_type=type(exc).__name__, retryable=True))
        phase = getattr(migration, "phase", None)
        return self._emit(self._result(public_operation, "migration_result", migration_phase=phase if isinstance(phase, str) else "unknown", retryable=bool(getattr(migration, "retryable", True)), error_type=_safe_error_type(migration)))

    def save_persistent_key(self, provider_id: object, secret: object, hwnd: object) -> CredentialPolicyResult:
        return self._write_persistent("save_persistent_key", provider_id, secret, hwnd, allow_replace=False)

    def replace_persistent_key(self, provider_id: object, secret: object, hwnd: object) -> CredentialPolicyResult:
        return self._write_persistent("replace_persistent_key", provider_id, secret, hwnd, allow_replace=True)

    def delete_persistent_key(self, provider_id: object, hwnd: object) -> CredentialPolicyResult:
        operation = "delete_persistent_key"
        preflight = self._management_preflight(operation, provider_id, hwnd)
        if preflight:
            return preflight
        backend = self._safe_call("delete", provider_id)
        return self._emit(self._backend_result(operation, provider_id, backend))

    def reveal_persistent_key(self, provider_id: object, hwnd: object) -> CredentialPolicyResult:
        return self._reveal_or_copy("reveal_persistent_key", provider_id, hwnd)

    def authorize_copy_persistent_key(self, provider_id: object, hwnd: object) -> CredentialPolicyResult:
        return self._reveal_or_copy("authorize_copy_persistent_key", provider_id, hwnd)

    def begin_legacy_migration(self, hwnd: object) -> CredentialPolicyResult:
        return self._run_migration("begin_legacy_migration", hwnd, "first_run")

    def continue_legacy_migration_and_cleanup(self, hwnd: object) -> CredentialPolicyResult:
        return self._run_migration("continue_legacy_migration_and_cleanup", hwnd, "restart_verify_and_cleanup")

    def _write_persistent(self, operation: str, provider_id: object, secret: object, hwnd: object, *, allow_replace: bool) -> CredentialPolicyResult:
        if not _valid_provider(provider_id) or not _valid_secret(secret):
            return self._emit(self._result(operation, "invalid_input", None))
        preflight = self._management_preflight(operation, provider_id, hwnd)
        if preflight:
            return preflight
        backend = self._safe_call("write", provider_id, secret, allow_replace=allow_replace)
        return self._emit(self._backend_result(operation, provider_id, backend))

    def _reveal_or_copy(self, operation: str, provider_id: object, hwnd: object) -> CredentialPolicyResult:
        preflight = self._management_preflight(operation, provider_id, hwnd)
        if preflight:
            return preflight
        backend = self._safe_call("read", provider_id)
        if _result_status(backend) != "found":
            return self._emit(self._backend_result(operation, provider_id, backend))
        secret = getattr(backend, "secret", None)
        if not _valid_secret(secret):
            return self._emit(self._result(operation, "backend_invalid_data", provider_id, backend_status="invalid_data", error_type="InvalidBackendSecret"))
        return self._emit(self._result(operation, "authorized", provider_id, backend_status="found", lease=CredentialUseLease(provider_id, secret, one_shot=True)))

    def _run_migration(self, operation: str, hwnd: object, method_name: str) -> CredentialPolicyResult:
        thread_error = self._require_ui_thread(operation, None)
        if thread_error:
            return thread_error
        hwnd_error = self._require_hwnd(operation, None, hwnd)
        if hwnd_error:
            return hwnd_error
        consent = self._fresh_consent(operation, None, hwnd)
        if consent is not None:
            return consent
        try:
            migration = getattr(self._migration_engine, method_name)()
        except Exception as exc:
            return self._emit(self._result(operation, "migration_error", error_type=type(exc).__name__, retryable=True))
        phase = getattr(migration, "phase", None)
        retryable = bool(getattr(migration, "retryable", True))
        return self._emit(self._result(operation, "migration_result", migration_phase=phase if isinstance(phase, str) else "unknown", retryable=retryable, error_type=_safe_error_type(migration)))

    def _management_preflight(self, operation: str, provider_id: object, hwnd: object) -> Optional[CredentialPolicyResult]:
        if not _valid_provider(provider_id):
            return self._emit(self._result(operation, "invalid_input", None))
        thread_error = self._require_ui_thread(operation, provider_id)
        if thread_error:
            return thread_error
        hwnd_error = self._require_hwnd(operation, provider_id, hwnd)
        if hwnd_error:
            return hwnd_error
        return self._fresh_consent(operation, provider_id, hwnd)

    def _read_persistent_lease(self, operation: str, provider_id: str) -> CredentialPolicyResult:
        backend = self._safe_call("read", provider_id)
        if _result_status(backend) != "found":
            return self._emit(self._backend_result(operation, provider_id, backend))
        secret = getattr(backend, "secret", None)
        if not _valid_secret(secret):
            return self._emit(self._result(operation, "backend_invalid_data", provider_id, backend_status="invalid_data", error_type="InvalidBackendSecret"))
        return self._emit(self._result(operation, "authorized", provider_id, backend_status="found", lease=CredentialUseLease(provider_id, secret)))

    def _fresh_consent(self, operation: str, provider_id: Optional[str], hwnd: int) -> Optional[CredentialPolicyResult]:
        availability = self._safe_hello_call("check_availability", hwnd)
        availability_status = _result_status(availability)
        if availability_status != "available":
            return self._emit(self._result(operation, "hello_unavailable", provider_id, hello_status=availability_status, error_type=_safe_error_type(availability)))
        verification = self._safe_hello_call(
            "request_verification",
            hwnd,
            "PixTagra is verifying your identity for a credential security action.",
        )
        verification_status = _result_status(verification)
        if verification_status != "verified":
            return self._emit(self._result(operation, "consent_not_verified", provider_id, hello_status=verification_status, error_type=_safe_error_type(verification)))
        return None

    def _require_ui_thread(self, operation: str, provider_id: Optional[str]) -> Optional[CredentialPolicyResult]:
        try:
            allowed = self._thread_checker() if self._thread_checker is not None else threading.get_ident() == self._ui_thread_id
        except Exception:
            allowed = False
        if allowed:
            return None
        return self._emit(self._result(operation, "wrong_thread", provider_id, error_type="WrongThread"))

    def _require_hwnd(self, operation: str, provider_id: Optional[str], hwnd: object) -> Optional[CredentialPolicyResult]:
        if _valid_hwnd(hwnd):
            return None
        return self._emit(self._result(operation, "invalid_hwnd", provider_id, error_type="InvalidHwnd"))

    def _safe_hello_call(self, method_name: str, *args: object) -> Any:
        try:
            return getattr(self._hello_backend, method_name)(*args)
        except Exception as exc:
            return _FailureResult("error", type(exc).__name__)

    def _safe_call(self, method_name: str, *args: object, **kwargs: object) -> Any:
        try:
            return getattr(self._credential_backend, method_name)(*args, **kwargs)
        except Exception as exc:
            return _FailureResult("error", type(exc).__name__)

    def _backend_result(self, operation: str, provider_id: str, backend: Any) -> CredentialPolicyResult:
        status = _result_status(backend)
        return self._result(
            operation,
            "backend_result",
            provider_id,
            backend_status=status,
            persistent_operations_allowed=status in {"stored", "already_present", "deleted", "already_absent", "found"},
            retryable=status not in {"conflict", "invalid_input"},
            error_type=_safe_error_type(backend),
        )

    @staticmethod
    def _result(
        operation: str,
        status: str,
        provider_id: Optional[str] = None,
        *,
        hello_status: Optional[str] = None,
        backend_status: Optional[str] = None,
        migration_phase: Optional[str] = None,
        persistent_operations_allowed: bool = False,
        session_only_allowed: bool = True,
        used_session_key: bool = False,
        retryable: bool = False,
        error_type: Optional[str] = None,
        lease: Optional[CredentialUseLease] = None,
        challenge: Optional[CredentialConsentChallenge] = None,
    ) -> CredentialPolicyResult:
        return CredentialPolicyResult(
            operation=operation,
            status=status,
            provider_id=provider_id if _valid_provider(provider_id) else None,
            hello_status=hello_status,
            backend_status=backend_status,
            migration_phase=migration_phase,
            persistent_operations_allowed=persistent_operations_allowed,
            session_only_allowed=session_only_allowed,
            used_session_key=used_session_key,
            retryable=retryable,
            error_type=error_type,
            lease=lease,
            challenge=challenge,
        )

    def _emit(self, result: CredentialPolicyResult) -> CredentialPolicyResult:
        if self._event_sink is None:
            return result
        event = CredentialPolicyEvent(
            operation=result.operation,
            provider_id=result.provider_id,
            status=result.status,
            hello_status=result.hello_status,
            backend_status=result.backend_status,
            session_only=result.used_session_key,
            retryable=result.retryable,
            error_type=result.error_type,
        )
        try:
            self._event_sink(event)
        except Exception:
            pass
        return result


@dataclass(frozen=True)
class _FailureResult:
    status: str
    error_type: str


def _result_status(result: object) -> str:
    status = getattr(result, "status", None)
    return status if isinstance(status, str) else "error"


def _safe_error_type(result: object) -> Optional[str]:
    error_type = getattr(result, "error_type", None)
    return error_type if isinstance(error_type, str) else None


__all__ = [
    "APPROVED_PROVIDER_IDS",
    "CREDENTIAL_BLOB_MAX_BYTES",
    "CredentialPolicyCoordinator",
    "CredentialConsentChallenge",
    "CredentialPolicyEvent",
    "CredentialPolicyResult",
    "CredentialUseLease",
    "SessionCredentialStore",
]
