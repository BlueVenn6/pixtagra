"""Windows Credential Manager backend primitives for a future PixTagra migration.

This module is intentionally not wired into the active application vault yet.
It exposes a provider-scoped, testable backend and never loads Advapi32.dll at
module import time.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import sys
from typing import Optional, Protocol


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CRED_MAX_CREDENTIAL_BLOB_SIZE = 2560
ERROR_NOT_FOUND = 1168
ERROR_CALL_NOT_IMPLEMENTED = 120

APP_TARGET_PREFIX = "PixTagra/APIKey/v1"
# Active GUI providers can use Credential Manager.  ``zhipu`` remains here so
# a historical target can be explicitly removed, while legacy Vault parsing is
# intentionally limited by LEGACY_VAULT_PROVIDER_IDS below.
APPROVED_PROVIDER_IDS = ("gemini", "openai", "kimi", "qwen", "minimax", "zhipu")
LEGACY_VAULT_PROVIDER_IDS = ("gemini", "openai", "kimi", "zhipu")

LPBYTE = ctypes.POINTER(ctypes.c_ubyte)


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", LPBYTE),
    ]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", LPBYTE),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(CREDENTIAL_ATTRIBUTEW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


@dataclass(frozen=True)
class AdapterReadResult:
    """Low-level read outcome. Raw credential bytes are intentionally hidden."""

    status: str
    blob: Optional[bytes] = field(default=None, repr=False, compare=False)
    error_code: Optional[int] = None
    error_type: Optional[str] = None


@dataclass(frozen=True)
class AdapterOperationResult:
    """Low-level write/delete outcome without credential content."""

    success: bool
    error_code: Optional[int] = None
    error_type: Optional[str] = None


@dataclass(frozen=True)
class CredentialResult:
    """Provider-scoped result whose repr and str never expose a secret."""

    operation: str
    status: str
    provider_id: Optional[str]
    target: Optional[str]
    error_code: Optional[int] = None
    verified: bool = False
    rollback_attempted: bool = False
    rollback_succeeded: Optional[bool] = None
    error_type: Optional[str] = None
    secret: Optional[str] = field(default=None, repr=False, compare=False)


class CredentialManagerError(RuntimeError):
    """Sanitized exception type for callers that need an exception boundary."""

    def __init__(self, operation: str, provider_id: Optional[str], error_code: Optional[int] = None):
        self.operation = operation
        self.provider_id = provider_id
        self.error_code = error_code
        super().__init__(self._message())

    def _message(self) -> str:
        provider = self.provider_id if self.provider_id in APPROVED_PROVIDER_IDS else "unknown"
        if self.error_code is None:
            return f"Credential Manager {self.operation} failed for provider '{provider}'."
        return f"Credential Manager {self.operation} failed for provider '{provider}' (error {self.error_code})."


class WinCredAdapter(Protocol):
    """Small injectable boundary around the native Credential Manager calls."""

    available: bool

    def read_target(self, target: str) -> AdapterReadResult:
        ...

    def write_target(self, target: str, blob: bytes) -> AdapterOperationResult:
        ...

    def delete_target(self, target: str) -> AdapterOperationResult:
        ...


def _is_approved_provider(provider_id: object) -> bool:
    return isinstance(provider_id, str) and provider_id in APPROVED_PROVIDER_IDS


def credential_target(provider_id: str) -> str:
    """Build a deterministic target only for a supported non-local provider."""
    if not _is_approved_provider(provider_id):
        raise ValueError("unsupported PixTagra credential provider")
    return f"{APP_TARGET_PREFIX}/{provider_id}"


class _WinCredApiAdapter:
    """Delayed ctypes wrapper for Advapi32 Credential Manager functions."""

    def __init__(self, dll=None, get_last_error=None, platform_name: Optional[str] = None):
        self.available = False
        self.load_error_code: Optional[int] = None
        self._cred_read = None
        self._cred_write = None
        self._cred_delete = None
        self._cred_free = None
        self._get_last_error = get_last_error or ctypes.get_last_error
        platform_name = platform_name or sys.platform
        if platform_name != "win32":
            self.load_error_code = ERROR_CALL_NOT_IMPLEMENTED
            return

        try:
            native_dll = dll if dll is not None else ctypes.WinDLL("Advapi32.dll", use_last_error=True)
            self._configure(native_dll)
        except (AttributeError, OSError, TypeError):
            self.load_error_code = self._safe_last_error()
            return
        self.available = True

    def _configure(self, native_dll) -> None:
        self._cred_read = native_dll.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(PCREDENTIALW),
        ]
        self._cred_read.restype = wintypes.BOOL

        self._cred_write = native_dll.CredWriteW
        self._cred_write.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self._cred_write.restype = wintypes.BOOL

        self._cred_delete = native_dll.CredDeleteW
        self._cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._cred_delete.restype = wintypes.BOOL

        self._cred_free = native_dll.CredFree
        self._cred_free.argtypes = [ctypes.c_void_p]
        self._cred_free.restype = None

    def _safe_last_error(self) -> Optional[int]:
        try:
            return int(self._get_last_error())
        except Exception:
            return None

    def read_target(self, target: str) -> AdapterReadResult:
        if not self.available:
            return AdapterReadResult(status="unavailable", error_code=self.load_error_code)

        credential_pointer = PCREDENTIALW()
        allocated = False
        try:
            if not self._cred_read(target, CRED_TYPE_GENERIC, 0, ctypes.byref(credential_pointer)):
                error_code = self._safe_last_error()
                if error_code == ERROR_NOT_FOUND:
                    return AdapterReadResult(status="not_found", error_code=error_code)
                return AdapterReadResult(status="error", error_code=error_code)

            allocated = bool(credential_pointer)
            if not allocated:
                return AdapterReadResult(status="invalid_data", error_type="NullCredentialPointer")

            credential = credential_pointer.contents
            blob_size = int(credential.CredentialBlobSize)
            if blob_size > CRED_MAX_CREDENTIAL_BLOB_SIZE:
                return AdapterReadResult(status="invalid_data", error_type="CredentialBlobTooLarge")
            if blob_size and not bool(credential.CredentialBlob):
                return AdapterReadResult(status="invalid_data", error_type="NullCredentialBlob")
            blob = ctypes.string_at(credential.CredentialBlob, blob_size) if blob_size else b""
            return AdapterReadResult(status="found", blob=blob)
        except Exception as exc:
            return AdapterReadResult(status="invalid_data", error_type=type(exc).__name__)
        finally:
            if allocated:
                try:
                    self._cred_free(ctypes.cast(credential_pointer, ctypes.c_void_p))
                except Exception:
                    # CredFree has no documented recoverable result. Do not expose
                    # native exception text, which can contain sensitive process data.
                    pass

    def write_target(self, target: str, blob: bytes) -> AdapterOperationResult:
        if not self.available:
            return AdapterOperationResult(success=False, error_code=self.load_error_code)

        byte_buffer = None
        try:
            byte_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
            credential = CREDENTIALW()
            credential.Type = CRED_TYPE_GENERIC
            credential.TargetName = target
            credential.Comment = "PixTagra API key v1"
            credential.CredentialBlobSize = len(blob)
            credential.CredentialBlob = ctypes.cast(byte_buffer, LPBYTE)
            credential.Persist = CRED_PERSIST_LOCAL_MACHINE
            credential.UserName = "PixTagra"
            if self._cred_write(ctypes.byref(credential), 0):
                return AdapterOperationResult(success=True)
            return AdapterOperationResult(success=False, error_code=self._safe_last_error())
        except Exception as exc:
            return AdapterOperationResult(success=False, error_type=type(exc).__name__)
        finally:
            if byte_buffer is not None:
                for index in range(len(byte_buffer)):
                    byte_buffer[index] = 0

    def delete_target(self, target: str) -> AdapterOperationResult:
        if not self.available:
            return AdapterOperationResult(success=False, error_code=self.load_error_code)
        try:
            if self._cred_delete(target, CRED_TYPE_GENERIC, 0):
                return AdapterOperationResult(success=True)
            return AdapterOperationResult(success=False, error_code=self._safe_last_error())
        except Exception as exc:
            return AdapterOperationResult(success=False, error_type=type(exc).__name__)


class WindowsCredentialManagerBackend:
    """Provider-only Credential Manager operations with verified state changes."""

    def __init__(self, adapter: Optional[WinCredAdapter] = None, platform_name: Optional[str] = None):
        self._adapter: WinCredAdapter = adapter or _WinCredApiAdapter(platform_name=platform_name)

    def _result(
        self,
        operation: str,
        status: str,
        provider_id: Optional[str],
        target: Optional[str],
        *,
        error_code: Optional[int] = None,
        verified: bool = False,
        rollback_attempted: bool = False,
        rollback_succeeded: Optional[bool] = None,
        error_type: Optional[str] = None,
        secret: Optional[str] = None,
    ) -> CredentialResult:
        return CredentialResult(
            operation=operation,
            status=status,
            provider_id=provider_id if _is_approved_provider(provider_id) else None,
            target=target,
            error_code=error_code,
            verified=verified,
            rollback_attempted=rollback_attempted,
            rollback_succeeded=rollback_succeeded,
            error_type=error_type,
            secret=secret,
        )

    def _target_or_invalid(self, operation: str, provider_id: object):
        if not _is_approved_provider(provider_id):
            return None, self._result(operation, "invalid_input", None, None)
        return credential_target(provider_id), None

    def _is_available(self) -> bool:
        return bool(getattr(self._adapter, "available", False))

    def _unavailable(self, operation: str, provider_id: str, target: str) -> CredentialResult:
        return self._result(operation, "unavailable", provider_id, target)

    def _call_read(self, target: str) -> AdapterReadResult:
        try:
            result = self._adapter.read_target(target)
        except Exception as exc:
            return AdapterReadResult(status="error", error_type=type(exc).__name__)
        if not isinstance(result, AdapterReadResult):
            return AdapterReadResult(status="error", error_type="InvalidAdapterReadResult")
        return result

    def _call_write(self, target: str, blob: bytes) -> AdapterOperationResult:
        try:
            result = self._adapter.write_target(target, blob)
        except Exception as exc:
            return AdapterOperationResult(success=False, error_type=type(exc).__name__)
        if not isinstance(result, AdapterOperationResult):
            return AdapterOperationResult(success=False, error_type="InvalidAdapterOperationResult")
        return result

    def _call_delete(self, target: str) -> AdapterOperationResult:
        try:
            result = self._adapter.delete_target(target)
        except Exception as exc:
            return AdapterOperationResult(success=False, error_type=type(exc).__name__)
        if not isinstance(result, AdapterOperationResult):
            return AdapterOperationResult(success=False, error_type="InvalidAdapterOperationResult")
        return result

    def _read_for_operation(self, operation: str, provider_id: str, target: str):
        native = self._call_read(target)
        if native.status == "not_found":
            return self._result(operation, "not_found", provider_id, target, error_code=native.error_code), None
        if native.status == "unavailable":
            return self._result(operation, "unavailable", provider_id, target, error_code=native.error_code), None
        if native.status == "invalid_data":
            return self._result(
                operation, "invalid_data", provider_id, target,
                error_code=native.error_code, error_type=native.error_type,
            ), None
        if native.status != "found" or not isinstance(native.blob, bytes):
            return self._result(
                operation, "error", provider_id, target,
                error_code=native.error_code, error_type=native.error_type,
            ), None

        blob = native.blob
        if not blob or len(blob) > CRED_MAX_CREDENTIAL_BLOB_SIZE or b"\x00" in blob:
            return self._result(operation, "invalid_data", provider_id, target), None
        try:
            secret = blob.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return self._result(operation, "invalid_data", provider_id, target, error_type="UnicodeDecodeError"), None
        return self._result(operation, "found", provider_id, target, verified=True, secret=secret), blob

    def _validate_secret(self, operation: str, provider_id: str, target: str, secret: object):
        if not isinstance(secret, str) or not secret or "\x00" in secret:
            return self._result(operation, "invalid_input", provider_id, target), None
        try:
            encoded = secret.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return self._result(operation, "invalid_input", provider_id, target, error_type="UnicodeEncodeError"), None
        if len(encoded) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
            return self._result(operation, "invalid_input", provider_id, target), None
        return None, encoded

    def read(self, provider_id: str) -> CredentialResult:
        target, invalid = self._target_or_invalid("read", provider_id)
        if invalid:
            return invalid
        if not self._is_available():
            return self._unavailable("read", provider_id, target)
        result, _blob = self._read_for_operation("read", provider_id, target)
        return result

    def write(self, provider_id: str, secret: str, allow_replace: bool = False) -> CredentialResult:
        target, invalid = self._target_or_invalid("write", provider_id)
        if invalid:
            return invalid
        validation_error, expected_blob = self._validate_secret("write", provider_id, target, secret)
        if validation_error:
            return validation_error
        if not self._is_available():
            return self._unavailable("write", provider_id, target)

        current, previous_blob = self._read_for_operation("write", provider_id, target)
        if current.status == "found":
            if previous_blob == expected_blob:
                return self._result("write", "already_present", provider_id, target, verified=True)
            if not allow_replace:
                return self._result("write", "conflict", provider_id, target, verified=True)
            write_result = self._call_write(target, expected_blob)
            if not write_result.success:
                return self._result(
                    "write", "write_failed", provider_id, target,
                    error_code=write_result.error_code, error_type=write_result.error_type,
                )
            verified, readback_blob = self._read_for_operation("write", provider_id, target)
            if verified.status == "found" and readback_blob == expected_blob:
                return self._result("write", "stored", provider_id, target, verified=True)
            return self._rollback_replacement(provider_id, target, previous_blob, verified)

        if current.status != "not_found":
            return current

        write_result = self._call_write(target, expected_blob)
        if not write_result.success:
            return self._result(
                "write", "write_failed", provider_id, target,
                error_code=write_result.error_code, error_type=write_result.error_type,
            )
        verified, readback_blob = self._read_for_operation("write", provider_id, target)
        if verified.status == "found" and readback_blob == expected_blob:
            return self._result("write", "stored", provider_id, target, verified=True)
        return self._rollback_new_target(provider_id, target, verified)

    def _rollback_new_target(self, provider_id: str, target: str, verification_result: CredentialResult) -> CredentialResult:
        delete_result = self._call_delete(target)
        after_delete, _blob = self._read_for_operation("write", provider_id, target)
        rollback_succeeded = delete_result.success and after_delete.status == "not_found"
        return self._result(
            "write",
            "verification_failed" if rollback_succeeded else "rollback_failed",
            provider_id,
            target,
            error_code=verification_result.error_code or delete_result.error_code,
            rollback_attempted=True,
            rollback_succeeded=rollback_succeeded,
            error_type=verification_result.error_type or delete_result.error_type,
        )

    def _rollback_replacement(
        self,
        provider_id: str,
        target: str,
        previous_blob: bytes,
        verification_result: CredentialResult,
    ) -> CredentialResult:
        restore_result = self._call_write(target, previous_blob)
        restored, restored_blob = self._read_for_operation("write", provider_id, target)
        rollback_succeeded = restore_result.success and restored.status == "found" and restored_blob == previous_blob
        return self._result(
            "write",
            "verification_failed" if rollback_succeeded else "rollback_failed",
            provider_id,
            target,
            error_code=verification_result.error_code or restore_result.error_code,
            rollback_attempted=True,
            rollback_succeeded=rollback_succeeded,
            error_type=verification_result.error_type or restore_result.error_type,
        )

    def delete(self, provider_id: str) -> CredentialResult:
        target, invalid = self._target_or_invalid("delete", provider_id)
        if invalid:
            return invalid
        if not self._is_available():
            return self._unavailable("delete", provider_id, target)

        current, _blob = self._read_for_operation("delete", provider_id, target)
        if current.status == "not_found":
            return self._result("delete", "already_absent", provider_id, target, verified=True)
        if current.status != "found":
            return current

        delete_result = self._call_delete(target)
        if not delete_result.success:
            return self._result(
                "delete", "delete_failed", provider_id, target,
                error_code=delete_result.error_code, error_type=delete_result.error_type,
            )
        verified, _blob = self._read_for_operation("delete", provider_id, target)
        if verified.status == "not_found":
            return self._result("delete", "deleted", provider_id, target, verified=True)
        return self._result(
            "delete", "verification_failed", provider_id, target,
            error_code=verified.error_code, error_type=verified.error_type,
        )


__all__ = [
    "APPROVED_PROVIDER_IDS",
    "LEGACY_VAULT_PROVIDER_IDS",
    "APP_TARGET_PREFIX",
    "AdapterOperationResult",
    "AdapterReadResult",
    "CREDENTIALW",
    "CRED_MAX_CREDENTIAL_BLOB_SIZE",
    "CRED_PERSIST_LOCAL_MACHINE",
    "CRED_TYPE_GENERIC",
    "CredentialManagerError",
    "CredentialResult",
    "ERROR_NOT_FOUND",
    "FILETIME",
    "LPBYTE",
    "PCREDENTIALW",
    "WindowsCredentialManagerBackend",
    "credential_target",
]
