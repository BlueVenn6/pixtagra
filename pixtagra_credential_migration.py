"""Strict, injectable legacy Vault to Credential Manager migration primitives.

This module is deliberately not imported by the application. Callers must pass
explicit legacy paths, identity inputs, a Credential Manager backend, and file
operations. Importing it never reads a Vault, loads user paths, or calls WinCred.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol
import uuid

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # The active app also treats unavailable crypto as a safe failure.
    AESGCM = None

from pixtagra_credentials import (
    CRED_MAX_CREDENTIAL_BLOB_SIZE,
    CredentialResult,
    LEGACY_VAULT_PROVIDER_IDS,
)


LEGACY_SALT_SIZE = 32
LEGACY_NONCE_SIZE = 12
LEGACY_AEAD_TAG_SIZE = 16
LEGACY_PBKDF2_ITERATIONS = 100_000
LEGACY_KEY_SIZE = 32
STATE_SCHEMA_VERSION = 1
STATE_BACKEND = "windows_credential_manager"
STATE_FILENAME = "credential-migration-state.json"

STATE_PHASES = {
    "planned",
    "destination_verification_pending",
    "restart_verification_pending",
    "conflict",
    "migration_failed",
    "legacy_cleanup_pending",
    "legacy_cleanup_complete",
}
PROVIDER_STATUSES = {
    "stored",
    "already_present",
    "verified",
    "conflict",
    "write_failed",
    "read_failed",
    "missing",
}
SAFE_ERROR_STAGES = {
    "legacy_not_found",
    "missing_vault",
    "missing_salt",
    "invalid_salt_length",
    "vault_too_short",
    "decrypt_failed",
    "invalid_utf8",
    "invalid_json",
    "invalid_schema",
    "duplicate_provider",
    "unsupported_provider",
    "invalid_secret",
    "state_invalid",
    "state_write",
    "destination_read",
    "destination_write",
    "destination_conflict",
    "restart_provider_set",
    "restart_read",
    "restart_mismatch",
    "cleanup_vault_delete",
    "cleanup_salt_delete",
    "cleanup_destination_missing",
    "cleanup_state_write",
    "state_inconsistent",
}


@dataclass(frozen=True)
class LegacyVaultSource:
    vault_path: Path
    salt_path: Path
    username: str = field(repr=False)
    machine_name: str = field(repr=False)


@dataclass(frozen=True)
class LegacyVaultReadResult:
    status: str
    provider_ids: tuple[str, ...] = ()
    error_type: Optional[str] = None
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class MigrationStateReadResult:
    status: str
    state: Optional[Mapping[str, Any]] = field(default=None, repr=False, compare=False)
    error_stage: Optional[str] = None
    error_type: Optional[str] = None


@dataclass(frozen=True)
class MigrationStateWriteResult:
    status: str
    error_stage: Optional[str] = None
    error_type: Optional[str] = None
    previous_restored: Optional[bool] = None


@dataclass(frozen=True)
class MigrationResult:
    phase: str
    provider_statuses: Mapping[str, str] = field(default_factory=dict)
    conflict_provider_ids: tuple[str, ...] = ()
    cleanup_complete: bool = False
    restart_verification_required: bool = False
    retryable: bool = True
    error_stage: Optional[str] = None
    error_code: Optional[int] = None
    error_type: Optional[str] = None


class CredentialBackend(Protocol):
    def read(self, provider_id: str) -> CredentialResult:
        ...

    def write(self, provider_id: str, secret: str, allow_replace: bool = False) -> CredentialResult:
        ...


class MigrationFileOps(Protocol):
    def exists(self, path: Path) -> bool:
        ...

    def mkdir(self, path: Path) -> None:
        ...

    def read_bytes(self, path: Path) -> bytes:
        ...

    def open_exclusive(self, path: Path) -> int:
        ...

    def open_text(self, fd: int):
        ...

    def fsync(self, fd: int) -> None:
        ...

    def replace(self, source: Path, target: Path) -> None:
        ...

    def unlink(self, path: Path) -> None:
        ...


class LocalMigrationFileOps:
    """Filesystem boundary used by the migration engine and replaced in tests."""

    def exists(self, path: Path) -> bool:
        return Path(path).exists()

    def mkdir(self, path: Path) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def read_bytes(self, path: Path) -> bytes:
        return Path(path).read_bytes()

    def open_exclusive(self, path: Path) -> int:
        return os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def open_text(self, fd: int):
        return os.fdopen(fd, "w", encoding="utf-8", newline="\n")

    def fsync(self, fd: int) -> None:
        os.fsync(fd)

    def replace(self, source: Path, target: Path) -> None:
        os.replace(source, target)

    def unlink(self, path: Path) -> None:
        Path(path).unlink()


class _ObjectPairs(list):
    """Distinguishes JSON objects from arrays while retaining duplicate keys."""


def _validate_secret(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= CRED_MAX_CREDENTIAL_BLOB_SIZE


class StrictLegacyVaultDecoder:
    """Read the historical AES-GCM Vault format without creating or mutating files."""

    def __init__(self, source: LegacyVaultSource, file_ops: Optional[MigrationFileOps] = None):
        self._source = source
        self._file_ops = file_ops or LocalMigrationFileOps()

    def read(self) -> LegacyVaultReadResult:
        vault_exists = self._file_ops.exists(self._source.vault_path)
        salt_exists = self._file_ops.exists(self._source.salt_path)
        if not vault_exists and not salt_exists:
            return LegacyVaultReadResult(status="legacy_not_found")
        if not vault_exists:
            return LegacyVaultReadResult(status="missing_vault")
        if not salt_exists:
            return LegacyVaultReadResult(status="missing_salt")

        try:
            salt = self._file_ops.read_bytes(self._source.salt_path)
            if len(salt) != LEGACY_SALT_SIZE:
                return LegacyVaultReadResult(status="invalid_salt_length")
            vault_bytes = self._file_ops.read_bytes(self._source.vault_path)
        except Exception as exc:
            return LegacyVaultReadResult(status="decrypt_failed", error_type=type(exc).__name__)

        if len(vault_bytes) < LEGACY_NONCE_SIZE + LEGACY_AEAD_TAG_SIZE:
            return LegacyVaultReadResult(status="vault_too_short")
        if AESGCM is None:
            return LegacyVaultReadResult(status="decrypt_failed", error_type="CryptoUnavailable")

        try:
            seed = f"{self._source.username}@{self._source.machine_name}".encode("utf-8")
            key = hashlib.pbkdf2_hmac(
                "sha256",
                seed,
                salt,
                LEGACY_PBKDF2_ITERATIONS,
                dklen=LEGACY_KEY_SIZE,
            )
            plaintext = AESGCM(key).decrypt(
                vault_bytes[:LEGACY_NONCE_SIZE],
                vault_bytes[LEGACY_NONCE_SIZE:],
                None,
            )
        except Exception as exc:
            return LegacyVaultReadResult(status="decrypt_failed", error_type=type(exc).__name__)

        try:
            decoded = plaintext.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return LegacyVaultReadResult(status="invalid_utf8")
        try:
            parsed = json.loads(decoded, object_pairs_hook=_ObjectPairs)
        except json.JSONDecodeError:
            return LegacyVaultReadResult(status="invalid_json")
        if not isinstance(parsed, _ObjectPairs):
            return LegacyVaultReadResult(status="invalid_schema")

        keys: dict[str, str] = {}
        for provider_id, secret in parsed:
            if provider_id in keys:
                return LegacyVaultReadResult(status="duplicate_provider")
            if not isinstance(provider_id, str) or provider_id not in LEGACY_VAULT_PROVIDER_IDS:
                return LegacyVaultReadResult(status="unsupported_provider")
            if isinstance(secret, (_ObjectPairs, list)):
                return LegacyVaultReadResult(status="invalid_schema")
            if not _validate_secret(secret):
                return LegacyVaultReadResult(status="invalid_secret")
            keys[provider_id] = secret
        if not keys:
            return LegacyVaultReadResult(status="invalid_schema")
        provider_ids = tuple(sorted(keys))
        return LegacyVaultReadResult(status="valid", provider_ids=provider_ids, secrets=keys)


def _validate_state(data: object) -> Optional[dict[str, Any]]:
    if not isinstance(data, dict):
        return None
    required = {
        "schema_version",
        "backend",
        "migration_id",
        "phase",
        "provider_ids",
        "provider_statuses",
        "first_write_verified",
        "cleanup_pending",
        "cleanup_complete",
        "error_stage",
        "error_code",
    }
    if set(data) != required:
        return None
    if data["schema_version"] != STATE_SCHEMA_VERSION or data["backend"] != STATE_BACKEND:
        return None
    if not isinstance(data["migration_id"], str) or not data["migration_id"]:
        return None
    if data["phase"] not in STATE_PHASES:
        return None
    provider_ids = data["provider_ids"]
    if not isinstance(provider_ids, list) or not provider_ids or provider_ids != sorted(set(provider_ids)):
        return None
    if any(provider_id not in LEGACY_VAULT_PROVIDER_IDS for provider_id in provider_ids):
        return None
    statuses = data["provider_statuses"]
    if not isinstance(statuses, dict) or set(statuses) - set(provider_ids):
        return None
    if any(status not in PROVIDER_STATUSES for status in statuses.values()):
        return None
    if not all(isinstance(data[name], bool) for name in ("first_write_verified", "cleanup_pending", "cleanup_complete")):
        return None
    if data["error_stage"] is not None and data["error_stage"] not in SAFE_ERROR_STAGES:
        return None
    if data["error_code"] is not None and not isinstance(data["error_code"], int):
        return None
    return dict(data)


class MigrationStateStore:
    """Non-secret state storage with staged write, readback, replace, and restore."""

    def __init__(
        self,
        path: Path,
        file_ops: Optional[MigrationFileOps] = None,
        token_factory: Optional[Callable[[], str]] = None,
    ):
        self.path = Path(path)
        self._file_ops = file_ops or LocalMigrationFileOps()
        self._token_factory = token_factory or (lambda: uuid.uuid4().hex)

    def read(self) -> MigrationStateReadResult:
        if not self._file_ops.exists(self.path):
            return MigrationStateReadResult(status="not_found")
        try:
            decoded = self._file_ops.read_bytes(self.path).decode("utf-8", errors="strict")
            parsed = json.loads(decoded)
            state = _validate_state(parsed)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return MigrationStateReadResult(status="invalid", error_stage="state_invalid", error_type=type(exc).__name__)
        except Exception as exc:
            return MigrationStateReadResult(status="invalid", error_stage="state_invalid", error_type=type(exc).__name__)
        if state is None:
            return MigrationStateReadResult(status="invalid", error_stage="state_invalid")
        return MigrationStateReadResult(status="valid", state=state)

    def write(self, state: Mapping[str, Any]) -> MigrationStateWriteResult:
        validated = _validate_state(dict(state))
        if validated is None:
            return MigrationStateWriteResult(status="write_failed", error_stage="state_write", error_type="InvalidStateSchema")
        previous_bytes = None
        previous_exists = self._file_ops.exists(self.path)
        if previous_exists:
            try:
                previous_bytes = self._file_ops.read_bytes(self.path)
            except Exception as exc:
                return MigrationStateWriteResult(status="write_failed", error_stage="state_write", error_type=type(exc).__name__)

        temp_path: Optional[Path] = None
        published = False
        try:
            self._file_ops.mkdir(self.path.parent)
            temp_path = self.path.parent / f".pixtagra_migration_{self._token_factory()}.tmp"
            fd = self._file_ops.open_exclusive(temp_path)
            handle = None
            try:
                handle = self._file_ops.open_text(fd)
                handle.write(json.dumps(validated, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
                handle.flush()
                self._file_ops.fsync(handle.fileno())
            finally:
                if handle is not None:
                    handle.close()
                else:
                    os.close(fd)

            staged = self._parse_bytes(self._file_ops.read_bytes(temp_path))
            if staged != validated:
                raise ValueError("staged state verification failed")
            self._file_ops.replace(temp_path, self.path)
            temp_path = None
            published = True
            final = self.read()
            if final.status != "valid" or final.state != validated:
                restored = self._restore_previous(previous_exists, previous_bytes)
                return MigrationStateWriteResult(
                    status="write_failed",
                    error_stage="state_write",
                    error_type="FinalStateReadbackFailed",
                    previous_restored=restored,
                )
            return MigrationStateWriteResult(status="written")
        except Exception as exc:
            restored = None
            if published:
                restored = self._restore_previous(previous_exists, previous_bytes)
            return MigrationStateWriteResult(
                status="write_failed",
                error_stage="state_write",
                error_type=type(exc).__name__,
                previous_restored=restored,
            )
        finally:
            if temp_path is not None and self._file_ops.exists(temp_path):
                try:
                    self._file_ops.unlink(temp_path)
                except Exception:
                    pass

    def _parse_bytes(self, raw: bytes) -> dict[str, Any]:
        parsed = json.loads(raw.decode("utf-8", errors="strict"))
        validated = _validate_state(parsed)
        if validated is None:
            raise ValueError("invalid migration state")
        return validated

    def _restore_previous(self, previous_exists: bool, previous_bytes: Optional[bytes]) -> bool:
        try:
            if not previous_exists:
                if self._file_ops.exists(self.path):
                    self._file_ops.unlink(self.path)
                return not self._file_ops.exists(self.path)
            if previous_bytes is None:
                return False
            temp_path = self.path.parent / f".pixtagra_migration_{self._token_factory()}.tmp"
            fd = self._file_ops.open_exclusive(temp_path)
            handle = None
            try:
                handle = self._file_ops.open_text(fd)
                handle.write(previous_bytes.decode("utf-8", errors="strict"))
                handle.flush()
                self._file_ops.fsync(handle.fileno())
            finally:
                if handle is not None:
                    handle.close()
                else:
                    os.close(fd)
            self._file_ops.replace(temp_path, self.path)
            return True
        except Exception:
            return False


class VaultCredentialMigrationEngine:
    """Two-stage migration that never runs unless a caller explicitly invokes it."""

    def __init__(
        self,
        source: LegacyVaultSource,
        backend: CredentialBackend,
        state_store: MigrationStateStore,
        file_ops: Optional[MigrationFileOps] = None,
        migration_id_factory: Optional[Callable[[], str]] = None,
    ):
        self._source = source
        self._backend = backend
        self._state_store = state_store
        self._file_ops = file_ops or LocalMigrationFileOps()
        self._migration_id_factory = migration_id_factory or (lambda: uuid.uuid4().hex)

    def first_run(self) -> MigrationResult:
        state_read = self._state_store.read()
        if state_read.status == "invalid":
            return self._failure("state_invalid", error_type=state_read.error_type)
        if state_read.status == "valid" and state_read.state["phase"] == "legacy_cleanup_complete":
            if not self._file_ops.exists(self._source.vault_path) and not self._file_ops.exists(self._source.salt_path):
                return self._complete(state_read.state["provider_statuses"])
            return self._failure("state_inconsistent")
        if state_read.status == "valid" and state_read.state["phase"] in {
            "restart_verification_pending",
            "legacy_cleanup_pending",
        }:
            return MigrationResult(
                phase=state_read.state["phase"],
                provider_statuses=dict(state_read.state["provider_statuses"]),
                restart_verification_required=state_read.state["phase"] == "restart_verification_pending",
            )

        legacy = StrictLegacyVaultDecoder(self._source, self._file_ops).read()
        if legacy.status != "valid":
            return self._failure(legacy.status, error_type=legacy.error_type)
        migration_id = (
            state_read.state["migration_id"]
            if state_read.status == "valid"
            else self._migration_id_factory()
        )
        provider_statuses: dict[str, str] = {}
        for provider_id in legacy.provider_ids:
            destination = self._safe_backend_read(provider_id)
            if destination.status == "not_found":
                written = self._safe_backend_write(provider_id, legacy.secrets[provider_id])
                if written.status != "stored" or not written.verified:
                    provider_statuses[provider_id] = "write_failed"
                    return self._record_failure(
                        migration_id,
                        legacy.provider_ids,
                        provider_statuses,
                        "destination_write",
                        written,
                    )
                provider_statuses[provider_id] = "stored"
                continue
            if destination.status == "found" and destination.secret == legacy.secrets[provider_id]:
                provider_statuses[provider_id] = "already_present"
                continue
            if destination.status == "found":
                provider_statuses[provider_id] = "conflict"
                return self._record_conflict(migration_id, legacy.provider_ids, provider_statuses, provider_id)
            provider_statuses[provider_id] = "read_failed"
            return self._record_failure(
                migration_id,
                legacy.provider_ids,
                provider_statuses,
                "destination_read",
                destination,
            )

        state = self._state_payload(
            migration_id,
            "restart_verification_pending",
            legacy.provider_ids,
            provider_statuses,
            first_write_verified=True,
        )
        state_write = self._state_store.write(state)
        if state_write.status != "written":
            return self._failure("state_write", provider_statuses, error_type=state_write.error_type)
        return MigrationResult(
            phase="restart_verification_pending",
            provider_statuses=provider_statuses,
            restart_verification_required=True,
        )

    def restart_verify_and_cleanup(self) -> MigrationResult:
        state_read = self._state_store.read()
        if state_read.status != "valid":
            return self._failure("state_invalid", error_type=state_read.error_type)
        state = dict(state_read.state)
        phase = state["phase"]
        if phase == "legacy_cleanup_complete":
            if not self._file_ops.exists(self._source.vault_path) and not self._file_ops.exists(self._source.salt_path):
                return self._complete(state["provider_statuses"])
            return self._failure("state_inconsistent", state["provider_statuses"])
        if phase not in {"restart_verification_pending", "legacy_cleanup_pending"}:
            return self._failure("state_inconsistent", state["provider_statuses"])

        vault_exists = self._file_ops.exists(self._source.vault_path)
        salt_exists = self._file_ops.exists(self._source.salt_path)
        if phase == "legacy_cleanup_pending" and not vault_exists:
            return self._resume_cleanup_without_vault(state, salt_exists)
        if not vault_exists or not salt_exists:
            return self._failure("state_inconsistent", state["provider_statuses"])

        legacy = StrictLegacyVaultDecoder(self._source, self._file_ops).read()
        if legacy.status != "valid":
            return self._record_restart_failure(state, "restart_read", error_type=legacy.error_type)
        if tuple(state["provider_ids"]) != legacy.provider_ids:
            return self._record_restart_failure(state, "restart_provider_set")

        provider_statuses = dict(state["provider_statuses"])
        for provider_id in legacy.provider_ids:
            destination = self._safe_backend_read(provider_id)
            if destination.status != "found":
                provider_statuses[provider_id] = "missing" if destination.status == "not_found" else "read_failed"
                return self._record_restart_failure(
                    state,
                    "restart_read",
                    provider_statuses,
                    error_code=destination.error_code,
                    error_type=destination.error_type,
                )
            if destination.secret != legacy.secrets[provider_id]:
                provider_statuses[provider_id] = "conflict"
                return self._record_restart_failure(state, "restart_mismatch", provider_statuses, conflict=(provider_id,))
            provider_statuses[provider_id] = "verified"

        pending_state = self._state_payload(
            state["migration_id"],
            "legacy_cleanup_pending",
            legacy.provider_ids,
            provider_statuses,
            first_write_verified=True,
        )
        pending_write = self._state_store.write(pending_state)
        if pending_write.status != "written":
            return self._failure("state_write", provider_statuses, error_type=pending_write.error_type)
        return self._perform_cleanup(pending_state)

    def _resume_cleanup_without_vault(self, state: Mapping[str, Any], salt_exists: bool) -> MigrationResult:
        provider_statuses = dict(state["provider_statuses"])
        for provider_id in state["provider_ids"]:
            destination = self._safe_backend_read(provider_id)
            if destination.status != "found":
                return MigrationResult(
                    phase="legacy_cleanup_pending",
                    provider_statuses=provider_statuses,
                    error_stage="cleanup_destination_missing",
                    error_code=destination.error_code,
                    error_type=destination.error_type,
                )
        if salt_exists:
            return self._perform_cleanup(state)
        complete_state = self._state_payload(
            state["migration_id"],
            "legacy_cleanup_complete",
            tuple(state["provider_ids"]),
            provider_statuses,
            first_write_verified=True,
        )
        written = self._state_store.write(complete_state)
        if written.status != "written":
            return MigrationResult(
                phase="legacy_cleanup_pending",
                provider_statuses=provider_statuses,
                error_stage="cleanup_state_write",
                error_type=written.error_type,
            )
        return self._complete(provider_statuses)

    def _perform_cleanup(self, pending_state: Mapping[str, Any]) -> MigrationResult:
        provider_statuses = dict(pending_state["provider_statuses"])
        if self._file_ops.exists(self._source.vault_path):
            try:
                self._file_ops.unlink(self._source.vault_path)
            except Exception as exc:
                return MigrationResult(
                    phase="legacy_cleanup_pending",
                    provider_statuses=provider_statuses,
                    error_stage="cleanup_vault_delete",
                    error_type=type(exc).__name__,
                )
            if self._file_ops.exists(self._source.vault_path):
                return MigrationResult(
                    phase="legacy_cleanup_pending",
                    provider_statuses=provider_statuses,
                    error_stage="cleanup_vault_delete",
                )
        if self._file_ops.exists(self._source.salt_path):
            try:
                self._file_ops.unlink(self._source.salt_path)
            except Exception as exc:
                return MigrationResult(
                    phase="legacy_cleanup_pending",
                    provider_statuses=provider_statuses,
                    error_stage="cleanup_salt_delete",
                    error_type=type(exc).__name__,
                )
            if self._file_ops.exists(self._source.salt_path):
                return MigrationResult(
                    phase="legacy_cleanup_pending",
                    provider_statuses=provider_statuses,
                    error_stage="cleanup_salt_delete",
                )
        complete_state = self._state_payload(
            pending_state["migration_id"],
            "legacy_cleanup_complete",
            tuple(pending_state["provider_ids"]),
            provider_statuses,
            first_write_verified=True,
        )
        written = self._state_store.write(complete_state)
        if written.status != "written":
            return MigrationResult(
                phase="legacy_cleanup_pending",
                provider_statuses=provider_statuses,
                error_stage="cleanup_state_write",
                error_type=written.error_type,
            )
        return self._complete(provider_statuses)

    def _record_conflict(
        self,
        migration_id: str,
        provider_ids: tuple[str, ...],
        provider_statuses: Mapping[str, str],
        provider_id: str,
    ) -> MigrationResult:
        state = self._state_payload(migration_id, "conflict", provider_ids, provider_statuses)
        write = self._state_store.write(state)
        if write.status != "written":
            return self._failure("state_write", provider_statuses, error_type=write.error_type)
        return MigrationResult(
            phase="conflict",
            provider_statuses=dict(provider_statuses),
            conflict_provider_ids=(provider_id,),
            error_stage="destination_conflict",
        )

    def _record_failure(
        self,
        migration_id: str,
        provider_ids: tuple[str, ...],
        provider_statuses: Mapping[str, str],
        error_stage: str,
        credential_result: CredentialResult,
    ) -> MigrationResult:
        state = self._state_payload(
            migration_id,
            "migration_failed",
            provider_ids,
            provider_statuses,
            error_stage=error_stage,
            error_code=credential_result.error_code,
        )
        write = self._state_store.write(state)
        if write.status != "written":
            return self._failure("state_write", provider_statuses, error_type=write.error_type)
        return self._failure(
            error_stage,
            provider_statuses,
            error_code=credential_result.error_code,
            error_type=credential_result.error_type,
        )

    def _record_restart_failure(
        self,
        state: Mapping[str, Any],
        error_stage: str,
        provider_statuses: Optional[Mapping[str, str]] = None,
        *,
        conflict: tuple[str, ...] = (),
        error_code: Optional[int] = None,
        error_type: Optional[str] = None,
    ) -> MigrationResult:
        statuses = dict(provider_statuses or state["provider_statuses"])
        phase = "conflict" if conflict else "migration_failed"
        failed_state = self._state_payload(
            state["migration_id"],
            phase,
            tuple(state["provider_ids"]),
            statuses,
            first_write_verified=bool(state["first_write_verified"]),
            error_stage=error_stage,
            error_code=error_code,
        )
        written = self._state_store.write(failed_state)
        if written.status != "written":
            return self._failure("state_write", statuses, error_type=written.error_type)
        return MigrationResult(
            phase=phase,
            provider_statuses=statuses,
            conflict_provider_ids=conflict,
            error_stage=error_stage,
            error_code=error_code,
            error_type=error_type,
        )

    def _state_payload(
        self,
        migration_id: str,
        phase: str,
        provider_ids: tuple[str, ...],
        provider_statuses: Mapping[str, str],
        *,
        first_write_verified: bool = False,
        error_stage: Optional[str] = None,
        error_code: Optional[int] = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "backend": STATE_BACKEND,
            "migration_id": migration_id,
            "phase": phase,
            "provider_ids": list(provider_ids),
            "provider_statuses": dict(provider_statuses),
            "first_write_verified": first_write_verified,
            "cleanup_pending": phase in {"legacy_cleanup_pending", "legacy_cleanup_complete"},
            "cleanup_complete": phase == "legacy_cleanup_complete",
            "error_stage": error_stage,
            "error_code": error_code,
        }

    def _safe_backend_read(self, provider_id: str) -> CredentialResult:
        try:
            return self._backend.read(provider_id)
        except Exception as exc:
            return CredentialResult(
                operation="read",
                status="error",
                provider_id=provider_id,
                target=None,
                error_type=type(exc).__name__,
            )

    def _safe_backend_write(self, provider_id: str, secret: str) -> CredentialResult:
        try:
            return self._backend.write(provider_id, secret, allow_replace=False)
        except Exception as exc:
            return CredentialResult(
                operation="write",
                status="error",
                provider_id=provider_id,
                target=None,
                error_type=type(exc).__name__,
            )

    def _failure(
        self,
        error_stage: str,
        provider_statuses: Optional[Mapping[str, str]] = None,
        *,
        error_code: Optional[int] = None,
        error_type: Optional[str] = None,
    ) -> MigrationResult:
        return MigrationResult(
            phase="migration_failed",
            provider_statuses=dict(provider_statuses or {}),
            error_stage=error_stage,
            error_code=error_code,
            error_type=error_type,
        )

    def _complete(self, provider_statuses: Mapping[str, str]) -> MigrationResult:
        return MigrationResult(
            phase="legacy_cleanup_complete",
            provider_statuses=dict(provider_statuses),
            cleanup_complete=True,
            retryable=False,
        )


__all__ = [
    "LEGACY_AEAD_TAG_SIZE",
    "LEGACY_KEY_SIZE",
    "LEGACY_NONCE_SIZE",
    "LEGACY_PBKDF2_ITERATIONS",
    "LEGACY_SALT_SIZE",
    "LocalMigrationFileOps",
    "LegacyVaultReadResult",
    "LegacyVaultSource",
    "MigrationResult",
    "MigrationStateReadResult",
    "MigrationStateStore",
    "MigrationStateWriteResult",
    "STATE_FILENAME",
    "StrictLegacyVaultDecoder",
    "VaultCredentialMigrationEngine",
]
