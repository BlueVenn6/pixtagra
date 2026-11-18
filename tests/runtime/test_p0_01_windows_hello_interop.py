"""Fake-only contract tests for the P0-01-3A Windows Hello interop spike."""

from __future__ import annotations

import importlib
import importlib.util
import os
import queue
from dataclasses import replace
from pathlib import Path
import sys
import ctypes
import uuid

import pytest

import pixtagra_windows_hello as hello


SENSITIVE_MARKER = "SYNTHETIC_CONSENT_MESSAGE_DO_NOT_LOG"


class FakeHelloAdapter:
    """A test adapter that records arguments without exposing messages in repr."""

    def __init__(self, availability=None, verification=None):
        self.availability = availability or hello.NativeHelloOutcome("availability", "ok", enum_value=0)
        self.verification = verification or hello.NativeHelloOutcome("verification", "ok", enum_value=0)
        self.calls = []

    def __repr__(self):
        return f"<FakeHelloAdapter calls={len(self.calls)}>"

    def check_availability(self, hwnd):
        self.calls.append(("availability", hwnd))
        return self.availability

    def request_verification(self, hwnd, message):
        self.calls.append(("verification", hwnd, message))
        return replace(self.verification, ui_requested=True)


class RaisingHelloAdapter:
    def check_availability(self, hwnd):
        raise RuntimeError(SENSITIVE_MARKER)

    def request_verification(self, hwnd, message):
        raise RuntimeError(SENSITIVE_MARKER)


class FakeWinRTRequestRecorder:
    """Exercises the native request path without loading Windows DLLs or UI."""

    factory_pointer = 0x1100
    interop_pointer = 0x1200
    operation_pointer = 0x1300
    async_info_pointer = 0x1400

    def __init__(self, *, statuses=(hello.ASYNC_STATUS_COMPLETED,), result_value=0, window_values=(True,)):
        self.statuses = iter(statuses)
        self.result_value = result_value
        self.window_values = iter(window_values)
        self.created_strings = []
        self.deleted_strings = []
        self.initialize_calls = []
        self.uninitialize_calls = 0
        self.factory_iids = []
        self.request_args = []
        self.query_iids = []
        self.released = []
        self.closed = []
        self.canceled = []
        self._next_string = 0x2100

    @staticmethod
    def _set_pointer(argument, value):
        ctypes.cast(argument, ctypes.POINTER(ctypes.c_void_p)).contents.value = value

    @staticmethod
    def _guid_text(argument):
        guid = ctypes.cast(argument, ctypes.POINTER(hello.GUID)).contents
        return str(uuid.UUID(bytes_le=bytes(guid))).lower()

    def make_native(self, monkeypatch):
        native = object.__new__(hello._NativeWinRTApi)

        def ro_initialize(flags):
            self.initialize_calls.append(flags)
            return hello.S_OK

        def ro_uninitialize():
            self.uninitialize_calls += 1

        def create_string(text, length, output):
            handle = self._next_string
            self._next_string += 1
            self.created_strings.append((text, length, handle))
            self._set_pointer(output, handle)
            return hello.S_OK

        def delete_string(handle):
            self.deleted_strings.append(handle.value)
            return hello.S_OK

        def get_factory(class_name, iid, output):
            self.factory_iids.append(self._guid_text(iid))
            self._set_pointer(output, self.factory_pointer)
            return hello.S_OK

        def is_window(_hwnd):
            try:
                return next(self.window_values)
            except StopIteration:
                return True

        native._ro_initialize = ro_initialize
        native._ro_uninitialize = ro_uninitialize
        native._windows_create_string = create_string
        native._windows_delete_string = delete_string
        native._ro_get_activation_factory = get_factory
        native._is_window = is_window

        def vtable_call(pointer, index, argtypes, *args):
            pointer_value = pointer.value
            if pointer_value == self.factory_pointer and index == 0:
                self.query_iids.append(self._guid_text(args[0]))
                self._set_pointer(args[1], self.interop_pointer)
                return hello.S_OK
            if pointer_value == self.interop_pointer and index == 6:
                self.request_args.append((args[0].value, args[1].value, self._guid_text(args[2]), argtypes))
                self._set_pointer(args[3], self.operation_pointer)
                return hello.S_OK
            if pointer_value == self.operation_pointer and index == 0:
                self.query_iids.append(self._guid_text(args[0]))
                self._set_pointer(args[1], self.async_info_pointer)
                return hello.S_OK
            if pointer_value == self.async_info_pointer and index == 7:
                ctypes.cast(args[0], ctypes.POINTER(ctypes.c_int32)).contents.value = next(self.statuses)
                return hello.S_OK
            if pointer_value == self.async_info_pointer and index == 8:
                ctypes.cast(args[0], ctypes.POINTER(ctypes.c_int32)).contents.value = 0
                return hello.S_OK
            if pointer_value == self.operation_pointer and index == 8:
                ctypes.cast(args[0], ctypes.POINTER(ctypes.c_int32)).contents.value = self.result_value
                return hello.S_OK
            if index == 2:
                self.released.append(pointer_value)
                return hello.S_OK
            if pointer_value == self.async_info_pointer and index == 9:
                self.canceled.append(pointer_value)
                return hello.S_OK
            if pointer_value == self.async_info_pointer and index == 10:
                self.closed.append(pointer_value)
                return hello.S_OK
            raise AssertionError(f"unexpected vtable call pointer={pointer_value} index={index}")

        monkeypatch.setattr(hello._NativeWinRTApi, "_vtable_call", staticmethod(vtable_call))
        return native


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = tmp_path / "config"
    home.mkdir()
    config.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PSS_CONFIG_DIR", str(config))
    monkeypatch.setenv("PSS_CONFIG_FILE", str(config / "config.json"))


def _backend(adapter=None, platform_name="win32"):
    return hello.WindowsHelloConsentBackend(
        adapter=adapter or FakeHelloAdapter(),
        platform_name=platform_name,
    )


def test_module_exports_non_secret_result_contract():
    result = hello.HelloConsentResult("availability", "available", ui_requested=False)

    assert result.operation == "availability"
    assert result.status == "available"
    assert result.ui_requested is False
    assert SENSITIVE_MARKER not in repr(result)


def test_valid_hwnd_is_forwarded_to_injected_adapter():
    adapter = FakeHelloAdapter()

    result = _backend(adapter).check_availability(12345)

    assert result.status == "available"
    assert adapter.calls == [("availability", 12345)]


@pytest.mark.parametrize("hwnd", (0, -1, True, "123", 1 << (8 * __import__("ctypes").sizeof(__import__("ctypes").c_void_p))))
def test_invalid_hwnd_never_reaches_adapter(hwnd):
    adapter = FakeHelloAdapter()

    result = _backend(adapter).check_availability(hwnd)

    assert result.status == "error"
    assert result.error_type == "InvalidHwnd"
    assert adapter.calls == []


@pytest.mark.parametrize(
    ("enum_value", "expected"),
    ((0, "available"), (1, "device_not_present"), (2, "not_configured_for_user"), (3, "disabled_by_policy"), (4, "device_busy")),
)
def test_availability_enum_values_are_mapped_separately(enum_value, expected):
    adapter = FakeHelloAdapter(availability=hello.NativeHelloOutcome("availability", "ok", enum_value=enum_value))

    result = _backend(adapter).check_availability(77)

    assert result.operation == "availability"
    assert result.status == expected
    assert result.ui_requested is False


@pytest.mark.parametrize(
    ("enum_value", "expected"),
    (
        (0, "verified"),
        (1, "device_not_present"),
        (2, "not_configured_for_user"),
        (3, "disabled_by_policy"),
        (4, "device_busy"),
        (5, "retries_exhausted"),
        (6, "canceled"),
    ),
)
def test_verification_enum_values_keep_canceled_and_unavailable_distinct(enum_value, expected):
    adapter = FakeHelloAdapter(verification=hello.NativeHelloOutcome("verification", "ok", enum_value=enum_value))

    result = _backend(adapter).request_verification(88, SENSITIVE_MARKER)

    assert result.operation == "verification"
    assert result.status == expected
    assert result.ui_requested is True
    assert SENSITIVE_MARKER not in repr(result)


@pytest.mark.parametrize(
    ("outcome", "expected", "error_type"),
    (
        (hello.NativeHelloOutcome("availability", "interop_unavailable", hresult=-2147467263), "interop_unavailable", None),
        (hello.NativeHelloOutcome("availability", "unsupported_platform"), "unsupported_platform", None),
        (hello.NativeHelloOutcome("availability", "timeout"), "unavailable", "Timeout"),
        (hello.NativeHelloOutcome("availability", "error", hresult=-1, error_type="NativeFailure"), "error", "NativeFailure"),
        (hello.NativeHelloOutcome("availability", "ok", enum_value=99), "unavailable", "UnknownAvailabilityValue"),
    ),
)
def test_availability_failures_are_not_mapped_to_success(outcome, expected, error_type):
    result = _backend(FakeHelloAdapter(availability=outcome)).check_availability(99)

    assert result.status == expected
    assert result.error_type == error_type


@pytest.mark.parametrize(
    ("outcome", "expected", "error_type"),
        (
            (hello.NativeHelloOutcome("verification", "interop_unavailable", hresult=-2147467263), "interop_error", "InteropUnavailable"),
            (hello.NativeHelloOutcome("verification", "timeout"), "timeout", "Timeout"),
            (hello.NativeHelloOutcome("verification", "window_destroyed"), "window_destroyed", "WindowDestroyed"),
        (hello.NativeHelloOutcome("verification", "error", hresult=-1, error_type="NativeFailure"), "error", "NativeFailure"),
        (hello.NativeHelloOutcome("verification", "ok", enum_value=99), "error", "UnknownVerificationValue"),
    ),
)
def test_verification_failures_do_not_become_canceled_or_verified(outcome, expected, error_type):
    result = _backend(FakeHelloAdapter(verification=outcome)).request_verification(101, SENSITIVE_MARKER)

    assert result.status == expected
    assert result.error_type == error_type
    assert SENSITIVE_MARKER not in repr(result)


def test_fake_request_marks_ui_only_in_result_and_never_creates_a_window(tmp_path):
    adapter = FakeHelloAdapter()

    result = _backend(adapter).request_verification(44, SENSITIVE_MARKER)

    assert result.status == "verified"
    assert result.ui_requested is True
    assert adapter.calls == [("verification", 44, SENSITIVE_MARKER)]
    assert set(tmp_path.iterdir()) == {tmp_path / "home", tmp_path / "config"}


def test_non_windows_backend_is_explicitly_unsupported_without_calling_adapter():
    adapter = FakeHelloAdapter()

    result = _backend(adapter, platform_name="linux").check_availability(123)

    assert result.status == "unsupported_platform"
    assert result.ui_requested is False
    assert adapter.calls == []


def test_native_adapter_is_lazy_and_non_windows_safe(monkeypatch):
    module = importlib.reload(hello)
    adapter = module.WindowsHelloInteropAdapter(platform_name="linux")

    assert adapter.native_loaded is False
    assert adapter.check_availability(123).status == "unsupported_platform"
    assert adapter.native_loaded is False


def test_multiple_fake_checks_are_not_persisted_or_cached(tmp_path):
    adapter = FakeHelloAdapter()
    backend = _backend(adapter)

    first = backend.check_availability(12)
    second = backend.check_availability(12)

    assert first.status == second.status == "available"
    assert adapter.calls == [("availability", 12), ("availability", 12)]
    assert set(tmp_path.iterdir()) == {tmp_path / "home", tmp_path / "config"}


def test_results_and_fake_adapter_repr_are_redacted():
    adapter = FakeHelloAdapter()
    result = _backend(adapter).request_verification(9, SENSITIVE_MARKER)

    assert SENSITIVE_MARKER not in repr(result)
    assert SENSITIVE_MARKER not in repr(adapter)
    assert SENSITIVE_MARKER not in str(result)


def test_adapter_exception_message_is_reduced_to_exception_type():
    result = _backend(RaisingHelloAdapter()).request_verification(9, SENSITIVE_MARKER)

    assert result.status == "error"
    assert result.error_type == "RuntimeError"
    assert SENSITIVE_MARKER not in repr(result)
    assert SENSITIVE_MARKER not in str(result)


def test_native_request_uses_the_interop_abi_and_releases_every_resource(monkeypatch):
    recorder = FakeWinRTRequestRecorder()
    native = recorder.make_native(monkeypatch)

    result = native.request_verification(0x3456, "Non-secret consent test.", timeout_seconds=0.5)

    assert result.operation == "verification"
    assert result.status == "ok"
    assert result.enum_value == 0
    assert result.ui_requested is True
    assert recorder.initialize_calls == [hello.RO_INIT_SINGLETHREADED]
    assert recorder.uninitialize_calls == 1
    assert recorder.factory_iids == [hello.IID_USER_CONSENT_VERIFIER_STATICS]
    assert recorder.query_iids == [hello.IID_USER_CONSENT_VERIFIER_INTEROP, hello.IID_ASYNC_INFO]
    assert recorder.request_args == [
        (
            0x3456,
            recorder.created_strings[1][2],
            hello.IID_ASYNC_VERIFICATION,
            (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(hello.GUID), ctypes.POINTER(ctypes.c_void_p)),
        )
    ]
    assert [text for text, _length, _handle in recorder.created_strings] == [
        hello.RUNTIME_CLASS_USER_CONSENT_VERIFIER,
        "Non-secret consent test.",
    ]
    assert recorder.deleted_strings == [item[2] for item in reversed(recorder.created_strings)]
    assert recorder.closed == [recorder.async_info_pointer]
    assert recorder.canceled == []
    assert recorder.released == [
        recorder.async_info_pointer,
        recorder.operation_pointer,
        recorder.interop_pointer,
        recorder.factory_pointer,
    ]
    assert [event.stage for event in result.timeline][-2:] == ["async_close", "interfaces_released"]


def test_native_request_timeout_cancels_then_closes_and_releases(monkeypatch):
    recorder = FakeWinRTRequestRecorder(statuses=(hello.ASYNC_STATUS_STARTED, hello.ASYNC_STATUS_CANCELED))
    native = recorder.make_native(monkeypatch)

    result = native.request_verification(0x4567, "Non-secret timeout test.", timeout_seconds=0)

    assert result.status == "timeout"
    assert result.ui_requested is True
    assert result.local_timeout is True
    assert result.async_status_before_cancel == hello.ASYNC_STATUS_STARTED
    assert result.async_status_after_cancel == hello.ASYNC_STATUS_CANCELED
    assert recorder.canceled == [recorder.async_info_pointer]
    assert recorder.closed == [recorder.async_info_pointer]
    assert recorder.released == [
        recorder.async_info_pointer,
        recorder.operation_pointer,
        recorder.interop_pointer,
        recorder.factory_pointer,
    ]
    assert recorder.uninitialize_calls == 1


def test_native_request_stops_when_the_explicit_window_is_destroyed(monkeypatch):
    monotonic_values = iter((0.0, 0.0))

    def monotonic():
        try:
            return next(monotonic_values)
        except StopIteration:
            return 0.0

    monkeypatch.setattr(hello.time, "monotonic", monotonic)
    recorder = FakeWinRTRequestRecorder(statuses=(hello.ASYNC_STATUS_STARTED,), window_values=(False,))
    native = recorder.make_native(monkeypatch)

    result = native.request_verification(0x5678, "Non-secret window test.", timeout_seconds=1.0)

    assert result.status == "window_destroyed"
    assert result.ui_requested is True
    assert recorder.canceled == [recorder.async_info_pointer]
    assert recorder.closed == [recorder.async_info_pointer]


def test_native_adapter_routes_one_verified_request_through_an_injected_native_api():
    class RecordingNativeApi:
        def __init__(self):
            self.calls = []

        def resolve_desktop_owner_hwnd(self, hwnd):
            self.calls.append(("resolve", hwnd))
            return hello.DesktopOwnerResolution(
                owner_hwnd=hwnd,
                state=hello.HelloWindowState(
                    valid=True,
                    visible=True,
                    enabled=True,
                    is_foreground=True,
                    raw_valid=True,
                    root_valid=True,
                    root_owner_valid=True,
                    foreground_valid=True,
                    foreground_same_process=True,
                    foreground_root_matches_owner=True,
                ),
            )

        def request_verification(self, hwnd, message, timeout_seconds):
            self.calls.append(("request", hwnd, message, timeout_seconds))
            return hello.NativeHelloOutcome("verification", "ok", enum_value=0, ui_requested=True)

    native = RecordingNativeApi()
    adapter = hello.WindowsHelloInteropAdapter(
        platform_name="win32",
        native_api=native,
        verification_timeout_seconds=0.25,
    )
    result = hello.WindowsHelloConsentBackend(adapter=adapter, platform_name="win32").request_verification(9, "Non-secret request.")

    assert result.status == "verified"
    assert result.ui_requested is True
    assert native.calls == [
        ("resolve", 9),
        ("request", 9, "Non-secret request.", 0.25),
    ]


def test_module_import_does_not_read_isolated_config_or_create_files(tmp_path):
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    importlib.reload(hello)

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert before == after
    assert os.environ["PSS_CONFIG_DIR"]
    assert Path(os.environ["PSS_CONFIG_FILE"]).parent.exists()


def _verification_probe_module():
    probe_path = Path(__file__).with_name("stage2_windows_hello_verification_probe.py")
    spec = importlib.util.spec_from_file_location("pixtagra_hello_verification_probe_test", probe_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeProbeBackend:
    def __init__(self, availability="available", verification="verified", owner_states=None):
        self.availability = availability
        self.verification = verification
        self.calls = []
        self.owner_states = list(owner_states or [
            hello.HelloWindowState(
                valid=True,
                visible=True,
                enabled=True,
                is_foreground=True,
                raw_valid=True,
                root_valid=True,
                root_owner_valid=True,
                foreground_valid=True,
                foreground_same_process=True,
                foreground_root_matches_owner=True,
            )
        ])

    def check_availability(self, hwnd):
        self.calls.append(("availability", hwnd))
        return hello.HelloConsentResult("availability", self.availability, interop_verified=True)

    def request_verification(self, hwnd, message):
        self.calls.append(("verification", hwnd, message))
        return hello.HelloConsentResult("verification", self.verification, ui_requested=True)

    def inspect_window(self, hwnd):
        self.calls.append(("inspect_window", hwnd))
        return self.owner_states.pop(0) if len(self.owner_states) > 1 else self.owner_states[0]


@pytest.mark.parametrize("argv", ([], ["--request-verification"], ["--acknowledge-visible-prompt"]))
def test_verification_probe_requires_both_explicit_flags_before_requesting_ui(argv):
    module = _verification_probe_module()
    backend = FakeProbeBackend()

    report = module.run_probe(argv, backend_factory=lambda: backend)

    assert report["hello_prompt_requested"] == 0
    assert report["verification_status"] == "not_requested"
    assert report["hwnd_created"] is False
    assert backend.calls == []
    assert "hwnd" not in report


def test_verification_worker_does_not_request_when_availability_is_not_available():
    module = _verification_probe_module()
    backend = FakeProbeBackend(availability="device_busy")
    results = queue.Queue()

    module._verification_worker(lambda: backend, 0x2468, results)
    payload = results.get_nowait()

    assert payload["verification"].status == "not_requested"
    assert [entry[0] for entry in backend.calls] == ["availability"]


def test_window_state_is_redacted_and_requires_visible_foreground_owner():
    state = hello.HelloWindowState(valid=True, visible=True, is_foreground=True)

    assert state.valid is True
    assert state.visible is True
    assert state.is_foreground is True
    assert "0x" not in repr(state)


def test_adapter_uses_independent_availability_and_verification_timeouts():
    class TimeoutRecordingNative:
        def __init__(self):
            self.calls = []

        def is_window(self, _hwnd):
            return True

        def resolve_desktop_owner_hwnd(self, hwnd):
            return hello.DesktopOwnerResolution(
                owner_hwnd=hwnd,
                state=hello.HelloWindowState(
                    valid=True,
                    visible=True,
                    enabled=True,
                    is_foreground=True,
                    raw_valid=True,
                    root_valid=True,
                    root_owner_valid=True,
                    foreground_valid=True,
                    foreground_same_process=True,
                    foreground_root_matches_owner=True,
                ),
            )

        def check_availability(self, timeout_seconds):
            self.calls.append(("availability", timeout_seconds))
            return hello.NativeHelloOutcome("availability", "ok", enum_value=0, interop_verified=True)

        def request_verification(self, hwnd, message, timeout_seconds):
            self.calls.append(("verification", hwnd, message, timeout_seconds))
            return hello.NativeHelloOutcome("verification", "ok", enum_value=0, ui_requested=True)

    native = TimeoutRecordingNative()
    adapter = hello.WindowsHelloInteropAdapter(
        platform_name="win32",
        native_api=native,
        availability_timeout_seconds=0.01,
        verification_timeout_seconds=120.0,
    )

    assert adapter.check_availability(9).status == "ok"
    assert adapter.request_verification(9, "Non-secret request.").status == "ok"
    assert native.calls == [
        ("availability", 0.01),
        ("verification", 9, "Non-secret request.", 120.0),
    ]


def test_default_verification_probe_creates_no_tk_window_or_prompt():
    module = _verification_probe_module()

    class NoWindowTk:
        def Tk(self):
            raise AssertionError("default probe must not create a test window")

    report = module.run_probe([], tk_module=NoWindowTk())

    assert report["hello_prompt_requested"] == 0
    assert report["verification_status"] == "not_requested"
    assert report["hwnd_created"] is False
    assert report["error_type"] is None


def test_double_opt_in_uses_an_interactive_runner_once_without_direct_request():
    module = _verification_probe_module()
    calls = []

    def interactive_runner(**kwargs):
        calls.append(kwargs)
        return {
            "availability_status": "available",
            "hello_prompt_requested": 1,
            "verification_status": "verified",
        }

    report = module.run_probe(
        ["--request-verification", "--acknowledge-visible-prompt"],
        interactive_runner=interactive_runner,
    )

    assert report["hello_prompt_requested"] == 1
    assert report["verification_status"] == "verified"
    assert len(calls) == 1


def test_verification_worker_returns_result_through_queue_without_tk_wait():
    module = _verification_probe_module()
    results = queue.Queue()

    class WorkerBackend:
        def check_availability(self, _hwnd):
            return hello.HelloConsentResult("availability", "available", interop_verified=True)

        def request_verification(self, _hwnd, _message):
            return hello.HelloConsentResult("verification", "verified", ui_requested=True)

    module._verification_worker(lambda: WorkerBackend(), 9, results)
    item = results.get_nowait()

    assert item["availability"].status == "available"
    assert item["verification"].status == "verified"


def test_non_visible_or_non_foreground_owner_never_reaches_native_request():
    class OwnerStateNative:
        def __init__(self, state):
            self.state = state
            self.request_count = 0

        def resolve_desktop_owner_hwnd(self, hwnd):
            return hello.DesktopOwnerResolution(owner_hwnd=hwnd, state=self.state)

        def request_verification(self, _hwnd, _message, _timeout):
            self.request_count += 1
            return hello.NativeHelloOutcome("verification", "ok", enum_value=0)

    active_relationship = {
        "enabled": True,
        "raw_valid": True,
        "root_valid": True,
        "root_owner_valid": True,
        "foreground_valid": True,
        "foreground_same_process": True,
    }
    hidden = OwnerStateNative(hello.HelloWindowState(valid=True, visible=False, is_foreground=False, **active_relationship))
    hidden_result = hello.WindowsHelloInteropAdapter(platform_name="win32", native_api=hidden).request_verification(9, "Non-secret.")
    inactive = OwnerStateNative(hello.HelloWindowState(valid=True, visible=True, is_foreground=False, **active_relationship))
    inactive_result = hello.WindowsHelloInteropAdapter(platform_name="win32", native_api=inactive).request_verification(9, "Non-secret.")

    assert hidden_result.error_type == "OwnerNotVisible"
    assert inactive_result.error_type == "OwnerWindowNotActive"
    assert hidden.request_count == 0
    assert inactive.request_count == 0


def test_verification_mapping_preserves_local_timeout_and_redacted_timeline():
    result = hello.WindowsHelloConsentBackend._map_verification(
        hello.NativeHelloOutcome(
            "verification",
            "timeout",
            local_timeout=True,
            async_status_before_cancel=hello.ASYNC_STATUS_STARTED,
            async_status_after_cancel=hello.ASYNC_STATUS_CANCELED,
            timeline=[hello.HelloDiagnosticEvent(10, "cancel_requested")],
        )
    )

    assert result.status == "timeout"
    assert result.local_timeout is True
    assert result.async_status_before_cancel == hello.ASYNC_STATUS_STARTED
    assert result.async_status_after_cancel == hello.ASYNC_STATUS_CANCELED
    assert isinstance(result.timeline, tuple)
    assert "0x" not in repr(result)


def test_visible_window_uses_user_click_and_queue_delivery_without_blocking_tk():
    module = _verification_probe_module()
    backend = FakeProbeBackend()

    class ImmediateThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

        def join(self, timeout=None):
            return None

    class Widget:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.state = "normal"

        def pack(self, **_kwargs):
            return None

        def configure(self, **kwargs):
            self.state = kwargs.get("state", self.state)

    class Window:
        def __init__(self):
            self.destroyed = False
            self.events = []
            self.title_text = None
            self.close_handler = None
            self.button = None

        def title(self, text):
            self.title_text = text

        def geometry(self, _value):
            return None

        def resizable(self, _width, _height):
            return None

        def winfo_id(self):
            return 0x2468

        def protocol(self, _name, callback):
            self.close_handler = callback

        def after(self, _delay, callback):
            self.events.append(callback)

        def mainloop(self):
            self.button.kwargs["command"]()
            while self.events and not self.destroyed:
                self.events.pop(0)()

        def destroy(self):
            self.destroyed = True

    class VisibleTk:
        def __init__(self):
            self.window = Window()

        def Tk(self):
            return self.window

        def Label(self, _root, **kwargs):
            return Widget(**kwargs)

        def Button(self, _root, **kwargs):
            button = Widget(**kwargs)
            self.window.button = button
            return button

    tk_module = VisibleTk()
    report = module.run_interactive_probe(
        backend_factory=lambda: backend,
        tk_module=tk_module,
        thread_factory=ImmediateThread,
    )

    assert tk_module.window.title_text == "Windows Hello Verification Test"
    assert report["hello_prompt_requested"] == 1
    assert report["verification_status"] == "verified"
    assert report["hwnd_valid"] is True
    assert report["hwnd_visible"] is True
    assert report["hwnd_is_foreground"] is True
    assert [call[0] for call in backend.calls] == ["inspect_window", "availability", "verification"]


class FakeOwnerRelationNative:
    """Fake User32 relationship data; no real HWND or process is accessed."""

    def __init__(self, *, raw=11, root=21, root_owner=31, foreground=41, foreground_root=21, owner_pid=100, foreground_pid=100):
        self.raw = raw
        self.root = root
        self.root_owner = root_owner
        self.foreground = foreground
        self.foreground_root = foreground_root
        self.owner_pid = owner_pid
        self.foreground_pid = foreground_pid
        self.ancestor_calls = []

    @staticmethod
    def _value(handle):
        return handle.value if isinstance(handle, ctypes.c_void_p) else int(handle)

    @staticmethod
    def _pointer(value):
        return ctypes.c_void_p(value) if value else ctypes.c_void_p()

    def make_native(self):
        native = object.__new__(hello._NativeWinRTApi)
        native._is_window = lambda handle: int(self._value(handle) in {self.raw, self.root, self.root_owner, self.foreground, self.foreground_root})
        native._is_window_visible = lambda handle: int(self._value(handle) == self.root)
        native._is_window_enabled = lambda handle: int(self._value(handle) == self.root)
        native._get_parent = lambda handle: self._pointer(1 if self._value(handle) == self.raw and self.raw != self.root else 0)

        def get_ancestor(handle, flag):
            value = self._value(handle)
            self.ancestor_calls.append((value, flag))
            if flag == hello.GA_ROOT:
                return self._pointer(self.foreground_root if value == self.foreground else self.root)
            if flag == hello.GA_ROOTOWNER:
                return self._pointer(self.root_owner)
            return self._pointer(0)

        native._get_ancestor = get_ancestor
        native._get_foreground_window = lambda: self._pointer(self.foreground)
        native._get_current_process_id = lambda: 100

        def get_window_thread_process_id(handle, process_id):
            value = self._value(handle)
            pid = self.foreground_pid if value == self.foreground else self.owner_pid
            ctypes.cast(process_id, ctypes.POINTER(ctypes.c_uint32)).contents.value = pid
            return 1

        native._get_window_thread_process_id = get_window_thread_process_id
        return native


def test_resolve_desktop_owner_uses_ga_root_and_keeps_rootowner_diagnostic_only():
    relations = FakeOwnerRelationNative()

    resolution = relations.make_native().resolve_desktop_owner_hwnd(relations.raw)

    assert resolution.owner_hwnd == relations.root
    assert resolution.state.raw_valid is True
    assert resolution.state.raw_is_child is True
    assert resolution.state.root_valid is True
    assert resolution.state.root_owner_valid is True
    assert resolution.state.foreground_same_process is True
    assert resolution.state.foreground_root_matches_owner is True
    assert (relations.raw, hello.GA_ROOT) in relations.ancestor_calls
    assert (relations.root, hello.GA_ROOTOWNER) in relations.ancestor_calls
    assert str(relations.raw) not in repr(resolution)
    assert str(relations.root) not in repr(resolution)


def test_adapter_passes_resolved_root_when_foreground_raw_is_a_different_child():
    state = hello.HelloWindowState(
        valid=True,
        visible=True,
        enabled=True,
        is_foreground=True,
        raw_valid=True,
        root_valid=True,
        root_owner_valid=True,
        foreground_valid=True,
        foreground_same_process=True,
        foreground_root_matches_owner=True,
    )

    class ResolvingNative:
        def __init__(self):
            self.requested_owner = None

        def resolve_desktop_owner_hwnd(self, _raw):
            return hello.DesktopOwnerResolution(owner_hwnd=21, state=state)

        def request_verification(self, hwnd, _message, _timeout):
            self.requested_owner = hwnd
            return hello.NativeHelloOutcome("verification", "ok", enum_value=0, ui_requested=True)

    native = ResolvingNative()
    result = hello.WindowsHelloInteropAdapter(platform_name="win32", native_api=native).request_verification(11, "Non-secret.")

    assert result.status == "ok"
    assert native.requested_owner == 21


@pytest.mark.parametrize(
    ("changes", "expected_error"),
    (
        ({"foreground_same_process": False}, "OwnerWindowNotActive"),
        ({"foreground_root_matches_owner": False}, "OwnerWindowNotActive"),
        ({"visible": False}, "OwnerNotVisible"),
        ({"enabled": False}, "OwnerNotEnabled"),
        ({"valid": False}, "InvalidHwnd"),
    ),
)
def test_adapter_rejects_unsafe_resolved_owner_without_request(changes, expected_error):
    state = hello.HelloWindowState(
        valid=True,
        visible=True,
        enabled=True,
        is_foreground=True,
        raw_valid=True,
        root_valid=True,
        root_owner_valid=True,
        foreground_valid=True,
        foreground_same_process=True,
        foreground_root_matches_owner=True,
    )
    for name, value in changes.items():
        state = replace(state, **{name: value})

    class RejectingNative:
        def __init__(self):
            self.request_count = 0

        def resolve_desktop_owner_hwnd(self, _raw):
            return hello.DesktopOwnerResolution(owner_hwnd=21, state=state)

        def request_verification(self, *_args):
            self.request_count += 1
            return hello.NativeHelloOutcome("verification", "ok", enum_value=0)

    native = RejectingNative()
    result = hello.WindowsHelloInteropAdapter(platform_name="win32", native_api=native).request_verification(11, "Non-secret.")

    assert result.error_type == expected_error
    assert native.request_count == 0


def test_hello_module_does_not_use_focus_stealing_or_thread_attachment_apis():
    source = Path(hello.__file__).read_text(encoding="utf-8")

    assert "SetForegroundWindow" not in source
    assert "AttachThreadInput" not in source


def test_activation_gate_requires_a_matching_enabled_same_process_owner_root():
    module = _verification_probe_module()
    ready = hello.HelloWindowState(
        valid=True,
        visible=True,
        enabled=True,
        is_foreground=True,
        raw_valid=True,
        root_valid=True,
        root_owner_valid=True,
        foreground_valid=True,
        foreground_same_process=True,
        foreground_root_matches_owner=True,
    )

    assert module._owner_is_active(ready) is True
    assert module._owner_is_active(replace(ready, foreground_root_matches_owner=False, is_foreground=False)) is False
    assert module._owner_is_active(replace(ready, foreground_same_process=False, is_foreground=False)) is False
    assert module._owner_is_active(replace(ready, enabled=False)) is False


def test_interactive_probe_waits_for_owner_root_then_starts_one_worker():
    module = _verification_probe_module()
    inactive = hello.HelloWindowState(
        valid=True,
        visible=True,
        enabled=True,
        is_foreground=False,
        raw_valid=True,
        root_valid=True,
        root_owner_valid=True,
        foreground_valid=True,
        foreground_same_process=True,
        foreground_root_matches_owner=False,
    )
    active = replace(inactive, is_foreground=True, foreground_root_matches_owner=True)
    backend = FakeProbeBackend(owner_states=[inactive, active])

    class ImmediateThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

        def join(self, timeout=None):
            return None

    class Widget:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.state = "normal"

        def pack(self, **_kwargs):
            return None

        def configure(self, **kwargs):
            self.state = kwargs.get("state", self.state)

    class Window:
        def __init__(self):
            self.events = []
            self.button = None
            self.destroyed = False

        def title(self, _value):
            return None

        def geometry(self, _value):
            return None

        def resizable(self, _width, _height):
            return None

        def winfo_id(self):
            return 0x2468

        def protocol(self, _name, _callback):
            return None

        def after(self, _delay, callback):
            self.events.append(callback)

        def mainloop(self):
            self.button.kwargs["command"]()
            self.button.kwargs["command"]()
            while self.events and not self.destroyed:
                self.events.pop(0)()

        def destroy(self):
            self.destroyed = True

    class FakeTk:
        def __init__(self):
            self.window = Window()

        def Tk(self):
            return self.window

        def Label(self, _root, **kwargs):
            return Widget(**kwargs)

        def Button(self, _root, **kwargs):
            self.window.button = Widget(**kwargs)
            return self.window.button

    monotonic_values = iter((0.0, 0.1, 0.1))
    report = module.run_interactive_probe(
        backend_factory=lambda: backend,
        tk_module=FakeTk(),
        thread_factory=ImmediateThread,
        activation_timeout_ms=500,
        activation_interval_ms=50,
        monotonic=lambda: next(monotonic_values),
    )

    assert report["hello_prompt_requested"] == 1
    assert report["foreground_root_matches_owner"] is True
    assert [entry[0] for entry in backend.calls] == ["inspect_window", "inspect_window", "availability", "verification"]


def test_interactive_probe_never_starts_worker_after_activation_window_expires():
    module = _verification_probe_module()
    inactive = hello.HelloWindowState(
        valid=True,
        visible=True,
        enabled=True,
        is_foreground=False,
        raw_valid=True,
        root_valid=True,
        root_owner_valid=True,
        foreground_valid=True,
        foreground_same_process=True,
        foreground_root_matches_owner=False,
    )
    backend = FakeProbeBackend(owner_states=[inactive])

    class Window:
        def __init__(self):
            self.events = []
            self.button = None

        def title(self, _value):
            return None

        def geometry(self, _value):
            return None

        def resizable(self, _width, _height):
            return None

        def winfo_id(self):
            return 0x2468

        def protocol(self, _name, _callback):
            return None

        def after(self, _delay, callback):
            self.events.append(callback)

        def mainloop(self):
            self.button.kwargs["command"]()
            while self.events:
                self.events.pop(0)()

        def destroy(self):
            return None

    class FakeTk:
        def __init__(self):
            self.window = Window()

        def Tk(self):
            return self.window

        def Label(self, _root, **kwargs):
            return type(
                "Label",
                (),
                {"pack": lambda self, **_kwargs: None, "configure": lambda self, **_kwargs: None},
            )()

        def Button(self, _root, **kwargs):
            self.window.button = type(
                "Button",
                (),
                {"kwargs": kwargs, "pack": lambda self, **_kwargs: None, "configure": lambda self, **_kwargs: None},
            )()
            return self.window.button

    report = module.run_interactive_probe(
        backend_factory=lambda: backend,
        tk_module=FakeTk(),
        activation_timeout_ms=500,
        activation_interval_ms=50,
        monotonic=iter((0.0, 0.6)).__next__,
    )

    assert report["hello_prompt_requested"] == 0
    assert report["verification_status"] == "not_requested"
    assert report["error_type"] == "OwnerWindowNotActive"
    assert [entry[0] for entry in backend.calls] == ["inspect_window"]
