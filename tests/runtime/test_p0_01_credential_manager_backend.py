"""Isolated contract tests for the P0-01-1 Credential Manager backend.

All storage calls use FakeWinCredAdapter. These tests must never reach a real
Windows Credential Manager target, vault, config file, provider, or network.
"""

from __future__ import annotations

import ctypes
import importlib.util
from pathlib import Path
import sys

import pytest

import pixtagra_credentials as credentials


TEST_KEY = "TEST_KEY_DO_NOT_USE"
OTHER_KEY = "OTHER_TEST_KEY_DO_NOT_USE"


class FakeWinCredAdapter:
    """In-memory WinCred adapter whose repr and call history exclude secrets."""

    available = True

    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.read_plan = []
        self.write_plan = []
        self.delete_plan = []
        self.read_calls = []
        self.write_calls = []
        self.delete_calls = []

    def __repr__(self):
        return (
            "<FakeWinCredAdapter "
            f"reads={len(self.read_calls)} writes={len(self.write_calls)} "
            f"deletes={len(self.delete_calls)}>"
        )

    def read_target(self, target):
        self.read_calls.append(target)
        if self.read_plan:
            return self.read_plan.pop(0)
        if target not in self.entries:
            return credentials.AdapterReadResult(
                status="not_found", error_code=credentials.ERROR_NOT_FOUND
            )
        return credentials.AdapterReadResult(status="found", blob=self.entries[target])

    def write_target(self, target, blob):
        self.write_calls.append(target)
        result = self.write_plan.pop(0) if self.write_plan else credentials.AdapterOperationResult(success=True)
        if result.success:
            self.entries[target] = bytes(blob)
        return result

    def delete_target(self, target):
        self.delete_calls.append(target)
        result = self.delete_plan.pop(0) if self.delete_plan else credentials.AdapterOperationResult(success=True)
        if result.success:
            self.entries.pop(target, None)
        return result


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_dir = tmp_path / "config"
    vault_dir = home / ".pictureseo"
    for path in (home, config_dir, vault_dir):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PSS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PSS_CONFIG_FILE", str(config_dir / "config.json"))
    yield {"home": home, "config": config_dir, "vault": vault_dir}


def _target(provider="gemini"):
    return credentials.credential_target(provider)


def _backend(entries=None):
    adapter = FakeWinCredAdapter(entries)
    return credentials.WindowsCredentialManagerBackend(adapter=adapter), adapter


@pytest.mark.parametrize("provider", credentials.APPROVED_PROVIDER_IDS)
def test_provider_target_is_deterministic_and_scoped(provider):
    assert _target(provider) == f"PixTagra/APIKey/v1/{provider}"


@pytest.mark.parametrize("provider", ("ollama", "unknown", "gemini/other", "GEMINI", ""))
def test_unapproved_provider_and_target_injection_are_rejected(provider):
    backend, adapter = _backend()

    assert backend.read(provider).status == "invalid_input"
    assert backend.write(provider, TEST_KEY).status == "invalid_input"
    assert backend.delete(provider).status == "invalid_input"
    with pytest.raises(ValueError):
        credentials.credential_target(provider)
    assert adapter.read_calls == []
    assert adapter.write_calls == []
    assert adapter.delete_calls == []


@pytest.mark.parametrize(
    "secret",
    ("", None, 42, "contains\x00nul", "a" * 2561, "\u00e9" * 1281),
)
def test_invalid_secret_inputs_are_rejected_without_native_calls(secret):
    backend, adapter = _backend()

    result = backend.write("gemini", secret)

    assert result.status == "invalid_input"
    assert adapter.read_calls == []
    assert adapter.write_calls == []


def test_utf8_byte_length_is_enforced_without_normalizing_secret():
    backend, adapter = _backend()
    within_limit = "e" * 2560
    multibyte_within_limit = "\u00e9" * 1280
    multibyte_over_limit = "\u00e9" * 1281

    assert backend.write("gemini", within_limit).status == "stored"
    assert backend.write("openai", multibyte_within_limit).status == "stored"
    assert backend.write("kimi", multibyte_over_limit).status == "invalid_input"
    exact = "  deliberately untrimmed  "
    assert backend.write("zhipu", exact).status == "stored"
    assert backend.read("zhipu").secret == exact
    assert _target("kimi") not in adapter.entries


def test_read_missing_maps_to_not_found_with_win32_code():
    backend, adapter = _backend()

    result = backend.read("gemini")

    assert result.status == "not_found"
    assert result.error_code == credentials.ERROR_NOT_FOUND
    assert result.secret is None
    assert adapter.read_calls == [_target()]


def test_read_returns_exact_utf8_secret_without_nul_termination():
    secret = "prefix\x00not allowed"
    backend, _adapter = _backend({_target(): TEST_KEY.encode("utf-8")})

    result = backend.read("gemini")

    assert result.status == "found"
    assert result.secret == TEST_KEY
    assert result.verified is True
    assert secret != result.secret


@pytest.mark.parametrize(
    "adapter_result",
    (
        credentials.AdapterReadResult(status="found", blob=b""),
        credentials.AdapterReadResult(status="found", blob=b"\xff"),
        credentials.AdapterReadResult(status="found", blob=b"safe\x00value"),
        credentials.AdapterReadResult(status="found", blob=b"x" * 2561),
        credentials.AdapterReadResult(status="invalid_data", error_code=87),
        credentials.AdapterReadResult(status="error", error_code=5),
    ),
)
def test_read_rejects_invalid_or_unusable_native_blob(adapter_result):
    backend, adapter = _backend()
    adapter.read_plan = [adapter_result]

    result = backend.read("gemini")

    assert result.status in {"invalid_data", "error"}
    assert result.secret is None


def test_write_new_secret_requires_exact_readback():
    backend, adapter = _backend()

    result = backend.write("gemini", TEST_KEY)

    assert result.status == "stored"
    assert result.verified is True
    assert adapter.entries == {_target(): TEST_KEY.encode("utf-8")}
    assert adapter.write_calls == [_target()]
    assert adapter.read_calls == [_target(), _target()]


def test_write_same_secret_is_idempotent_without_write_call():
    backend, adapter = _backend({_target(): TEST_KEY.encode("utf-8")})

    result = backend.write("gemini", TEST_KEY)

    assert result.status == "already_present"
    assert result.verified is True
    assert adapter.write_calls == []


def test_write_different_secret_is_conflict_without_default_overwrite():
    backend, adapter = _backend({_target(): OTHER_KEY.encode("utf-8")})

    result = backend.write("gemini", TEST_KEY)

    assert result.status == "conflict"
    assert result.verified is True
    assert adapter.entries[_target()] == OTHER_KEY.encode("utf-8")
    assert adapter.write_calls == []


def test_explicit_replace_updates_only_requested_provider_after_readback():
    openai_target = _target("openai")
    backend, adapter = _backend(
        {_target(): OTHER_KEY.encode("utf-8"), openai_target: b"OPENAI_SYNTHETIC"}
    )

    result = backend.write("gemini", TEST_KEY, allow_replace=True)

    assert result.status == "stored"
    assert result.verified is True
    assert adapter.entries[_target()] == TEST_KEY.encode("utf-8")
    assert adapter.entries[openai_target] == b"OPENAI_SYNTHETIC"


def test_write_failure_does_not_report_success_or_change_other_provider():
    openai_target = _target("openai")
    backend, adapter = _backend({openai_target: b"OPENAI_SYNTHETIC"})
    adapter.write_plan = [credentials.AdapterOperationResult(success=False, error_code=5)]

    result = backend.write("gemini", TEST_KEY)

    assert result.status == "write_failed"
    assert _target() not in adapter.entries
    assert adapter.entries[openai_target] == b"OPENAI_SYNTHETIC"


def test_new_write_readback_missing_rolls_back_new_target():
    backend, adapter = _backend()
    adapter.read_plan = [
        credentials.AdapterReadResult(status="not_found", error_code=credentials.ERROR_NOT_FOUND),
        credentials.AdapterReadResult(status="not_found", error_code=credentials.ERROR_NOT_FOUND),
    ]

    result = backend.write("gemini", TEST_KEY)

    assert result.status == "verification_failed"
    assert result.rollback_attempted is True
    assert result.rollback_succeeded is True
    assert _target() not in adapter.entries
    assert adapter.delete_calls == [_target()]


def test_new_write_readback_mismatch_rolls_back_new_target():
    backend, adapter = _backend()
    adapter.read_plan = [
        credentials.AdapterReadResult(status="not_found", error_code=credentials.ERROR_NOT_FOUND),
        credentials.AdapterReadResult(status="found", blob=OTHER_KEY.encode("utf-8")),
    ]

    result = backend.write("gemini", TEST_KEY)

    assert result.status == "verification_failed"
    assert result.rollback_succeeded is True
    assert _target() not in adapter.entries


def test_new_write_verification_failure_reports_rollback_failure():
    backend, adapter = _backend()
    adapter.read_plan = [
        credentials.AdapterReadResult(status="not_found", error_code=credentials.ERROR_NOT_FOUND),
        credentials.AdapterReadResult(status="found", blob=OTHER_KEY.encode("utf-8")),
    ]
    adapter.delete_plan = [credentials.AdapterOperationResult(success=False, error_code=5)]

    result = backend.write("gemini", TEST_KEY)

    assert result.status == "rollback_failed"
    assert result.rollback_attempted is True
    assert result.rollback_succeeded is False


def test_replace_verification_failure_restores_previous_secret():
    backend, adapter = _backend({_target(): OTHER_KEY.encode("utf-8")})
    adapter.read_plan = [
        credentials.AdapterReadResult(status="found", blob=OTHER_KEY.encode("utf-8")),
        credentials.AdapterReadResult(status="found", blob=b"MISMATCH"),
    ]

    result = backend.write("gemini", TEST_KEY, allow_replace=True)

    assert result.status == "verification_failed"
    assert result.rollback_attempted is True
    assert result.rollback_succeeded is True
    assert adapter.entries[_target()] == OTHER_KEY.encode("utf-8")


def test_replace_verification_failure_reports_failed_restore():
    backend, adapter = _backend({_target(): OTHER_KEY.encode("utf-8")})
    adapter.read_plan = [
        credentials.AdapterReadResult(status="found", blob=OTHER_KEY.encode("utf-8")),
        credentials.AdapterReadResult(status="found", blob=b"MISMATCH"),
    ]
    adapter.write_plan = [
        credentials.AdapterOperationResult(success=True),
        credentials.AdapterOperationResult(success=False, error_code=5),
    ]

    result = backend.write("gemini", TEST_KEY, allow_replace=True)

    assert result.status == "rollback_failed"
    assert result.rollback_succeeded is False


def test_delete_absent_is_idempotent_and_does_not_touch_other_provider():
    openai_target = _target("openai")
    backend, adapter = _backend({openai_target: b"OPENAI_SYNTHETIC"})

    result = backend.delete("gemini")

    assert result.status == "already_absent"
    assert adapter.entries[openai_target] == b"OPENAI_SYNTHETIC"
    assert adapter.delete_calls == []


def test_delete_requires_verified_not_found_after_success():
    backend, adapter = _backend({_target(): TEST_KEY.encode("utf-8")})

    result = backend.delete("gemini")

    assert result.status == "deleted"
    assert result.verified is True
    assert _target() not in adapter.entries


def test_delete_failure_and_readback_still_present_are_not_success():
    backend, adapter = _backend({_target(): TEST_KEY.encode("utf-8")})
    adapter.delete_plan = [credentials.AdapterOperationResult(success=False, error_code=5)]

    result = backend.delete("gemini")

    assert result.status == "delete_failed"
    assert adapter.entries[_target()] == TEST_KEY.encode("utf-8")

    adapter.delete_plan = [credentials.AdapterOperationResult(success=True)]
    adapter.read_plan = [
        credentials.AdapterReadResult(status="found", blob=TEST_KEY.encode("utf-8")),
        credentials.AdapterReadResult(status="found", blob=TEST_KEY.encode("utf-8")),
    ]
    result = backend.delete("gemini")
    assert result.status == "verification_failed"


def test_result_adapter_and_exception_representations_do_not_leak_secret(capsys):
    backend, adapter = _backend({_target(): TEST_KEY.encode("utf-8")})
    result = backend.read("gemini")
    adapter.read_plan = [
        credentials.AdapterReadResult(status="error", error_type="RuntimeError")
    ]
    failed = backend.read("gemini")
    error = credentials.CredentialManagerError("read", "gemini", error_code=5)

    print(result)
    print(failed)
    print(adapter)
    print(error)
    captured = capsys.readouterr()

    assert TEST_KEY not in repr(result)
    assert TEST_KEY not in str(result)
    assert TEST_KEY not in repr(adapter)
    assert TEST_KEY not in str(error)
    assert TEST_KEY not in captured.out


def test_adapter_exceptions_are_sanitized_without_secret_text(capsys):
    backend, adapter = _backend()

    def raise_secret(_target):
        raise RuntimeError(TEST_KEY)

    adapter.read_target = raise_secret
    result = backend.read("gemini")
    print(result)
    captured = capsys.readouterr()

    assert result.status == "error"
    assert result.error_type == "RuntimeError"
    assert TEST_KEY not in repr(result)
    assert TEST_KEY not in captured.out


def test_non_windows_backend_is_unavailable_without_dll_load(monkeypatch):
    calls = []

    def forbidden_loader(*_args, **_kwargs):
        calls.append("WinDLL")
        raise AssertionError("native DLL must not load")

    monkeypatch.setattr(credentials.ctypes, "WinDLL", forbidden_loader, raising=False)
    backend = credentials.WindowsCredentialManagerBackend(platform_name="linux")

    assert backend.read("gemini").status == "unavailable"
    assert backend.write("gemini", TEST_KEY).status == "unavailable"
    assert backend.delete("gemini").status == "unavailable"
    assert calls == []


def test_module_import_does_not_load_advapi32(monkeypatch):
    calls = []

    def forbidden_loader(*_args, **_kwargs):
        calls.append("WinDLL")
        raise AssertionError("import must not load Advapi32.dll")

    monkeypatch.setattr(ctypes, "WinDLL", forbidden_loader, raising=False)
    module_path = Path(credentials.__file__)
    module_name = "pixtagra_credentials_import_probe"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    probe_module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = probe_module
    try:
        assert spec.loader is not None
        spec.loader.exec_module(probe_module)
    finally:
        sys.modules.pop(module_name, None)

    assert calls == []


class _NativeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.callback(*args)


class _FakeNativeDll:
    def __init__(self, blob, error_code=0):
        self.blob_buffer = (ctypes.c_ubyte * len(blob))(*blob)
        self.credential = credentials.CREDENTIALW()
        self.credential.Type = credentials.CRED_TYPE_GENERIC
        self.credential.CredentialBlobSize = len(blob)
        self.credential.CredentialBlob = ctypes.cast(self.blob_buffer, credentials.LPBYTE)
        self.credential_pointer = ctypes.pointer(self.credential)
        self.error_code = error_code
        self.CredReadW = _NativeFunction(self._read)
        self.CredWriteW = _NativeFunction(lambda *_args: 1)
        self.CredDeleteW = _NativeFunction(lambda *_args: 1)
        self.CredFree = _NativeFunction(lambda *_args: None)

    def _read(self, _target, _type, _flags, output_pointer):
        pointer = ctypes.cast(output_pointer, ctypes.POINTER(credentials.PCREDENTIALW))
        pointer[0] = self.credential_pointer
        return 1


@pytest.mark.parametrize(
    "blob, expected_status",
    ((b"exact", "found"), (b"a\x00b", "found"), (b"\xff", "found")),
)
def test_native_wrapper_uses_blob_size_sets_signatures_and_frees_once(blob, expected_status):
    fake_dll = _FakeNativeDll(blob)
    adapter = credentials._WinCredApiAdapter(
        dll=fake_dll, get_last_error=lambda: fake_dll.error_code, platform_name="win32"
    )

    result = adapter.read_target(_target())

    assert result.status == expected_status
    assert result.blob == blob
    assert len(fake_dll.CredFree.calls) == 1
    assert fake_dll.CredReadW.argtypes is not None
    assert fake_dll.CredReadW.restype is not None
    assert fake_dll.CredWriteW.argtypes is not None
    assert fake_dll.CredDeleteW.argtypes is not None


def test_utf8_decode_failure_happens_after_native_credential_is_freed():
    fake_dll = _FakeNativeDll(b"\xff")
    adapter = credentials._WinCredApiAdapter(
        dll=fake_dll, get_last_error=lambda: 0, platform_name="win32"
    )
    backend = credentials.WindowsCredentialManagerBackend(adapter=adapter)

    result = backend.read("gemini")

    assert result.status == "invalid_data"
    assert len(fake_dll.CredFree.calls) == 1


def test_native_wrapper_frees_allocated_result_when_blob_pointer_is_invalid():
    fake_dll = _FakeNativeDll(b"a")
    fake_dll.credential.CredentialBlob = credentials.LPBYTE()
    fake_dll.credential.CredentialBlobSize = 1
    adapter = credentials._WinCredApiAdapter(
        dll=fake_dll, get_last_error=lambda: 0, platform_name="win32"
    )

    result = adapter.read_target(_target())

    assert result.status == "invalid_data"
    assert len(fake_dll.CredFree.calls) == 1


@pytest.mark.parametrize(
    "error_code, expected_status",
    ((credentials.ERROR_NOT_FOUND, "not_found"), (5, "error")),
)
def test_native_wrapper_propagates_credread_last_error(error_code, expected_status):
    fake_dll = _FakeNativeDll(b"unused", error_code=error_code)
    fake_dll.CredReadW = _NativeFunction(lambda *_args: 0)
    adapter = credentials._WinCredApiAdapter(
        dll=fake_dll, get_last_error=lambda: error_code, platform_name="win32"
    )

    result = adapter.read_target(_target())

    assert result.status == expected_status
    assert result.error_code == error_code
    assert len(fake_dll.CredFree.calls) == 0


def test_native_wrapper_writes_generic_local_machine_utf8_blob_without_nul():
    fake_dll = _FakeNativeDll(b"unused")
    captured = {}

    def capture_write(credential_pointer, flags):
        credential = ctypes.cast(
            credential_pointer, ctypes.POINTER(credentials.CREDENTIALW)
        ).contents
        captured.update(
            {
                "target": credential.TargetName,
                "type": credential.Type,
                "persist": credential.Persist,
                "size": credential.CredentialBlobSize,
                "blob": ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize),
                "flags": flags,
            }
        )
        return 1

    fake_dll.CredWriteW = _NativeFunction(capture_write)
    adapter = credentials._WinCredApiAdapter(
        dll=fake_dll, get_last_error=lambda: 0, platform_name="win32"
    )

    result = adapter.write_target(_target(), TEST_KEY.encode("utf-8"))

    assert result.success is True
    assert captured == {
        "target": _target(),
        "type": credentials.CRED_TYPE_GENERIC,
        "persist": credentials.CRED_PERSIST_LOCAL_MACHINE,
        "size": len(TEST_KEY.encode("utf-8")),
        "blob": TEST_KEY.encode("utf-8"),
        "flags": 0,
    }


def test_native_wrapper_delete_uses_only_scoped_target_and_propagates_error():
    fake_dll = _FakeNativeDll(b"unused", error_code=5)
    fake_dll.CredDeleteW = _NativeFunction(lambda *_args: 0)
    adapter = credentials._WinCredApiAdapter(
        dll=fake_dll, get_last_error=lambda: 5, platform_name="win32"
    )

    result = adapter.delete_target(_target("openai"))

    assert result.success is False
    assert result.error_code == 5
    target, credential_type, flags = fake_dll.CredDeleteW.calls[0]
    assert target == _target("openai")
    assert credential_type == credentials.CRED_TYPE_GENERIC
    assert flags == 0
