"""Windows Hello consent interop spike with no application integration.

Native verification is available only through an explicit direct caller. The
application does not import this module, and the default probe never invokes
RequestVerificationForWindowAsync.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import sys
import time
from typing import Optional, Protocol
import uuid


RUNTIME_CLASS_USER_CONSENT_VERIFIER = "Windows.Security.Credentials.UI.UserConsentVerifier"
IID_USER_CONSENT_VERIFIER_STATICS = "af4f3f91-564c-4ddc-b8b5-973447627c65"
IID_USER_CONSENT_VERIFIER_INTEROP = "39e050c3-4e74-441a-8dc0-b81104df949c"
IID_ASYNC_AVAILABILITY = "ddd384f3-d818-5d83-ab4b-32119c28587c"
IID_ASYNC_VERIFICATION = "fd596ffd-2318-558f-9dbe-d21df43764a5"
IID_ASYNC_INFO = "00000036-0000-0000-c000-000000000046"

RO_INIT_SINGLETHREADED = 0
S_OK = 0
S_FALSE = 1
ASYNC_STATUS_STARTED = 0
ASYNC_STATUS_COMPLETED = 1
ASYNC_STATUS_CANCELED = 2
ASYNC_STATUS_ERROR = 3
DEFAULT_AVAILABILITY_TIMEOUT_SECONDS = 2.0
DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 120.0
POST_CANCEL_SETTLE_SECONDS = 2.0
GA_ROOT = 2
GA_ROOTOWNER = 3

AVAILABILITY_STATUS = {
    0: "available",
    1: "device_not_present",
    2: "not_configured_for_user",
    3: "disabled_by_policy",
    4: "device_busy",
}
VERIFICATION_STATUS = {
    0: "verified",
    1: "device_not_present",
    2: "not_configured_for_user",
    3: "disabled_by_policy",
    4: "device_busy",
    5: "retries_exhausted",
    6: "canceled",
}


class GUID(ctypes.Structure):
    """Win32 GUID layout verified against the Windows SDK ABI declarations."""

    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_text(cls, value: str) -> "GUID":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


@dataclass(frozen=True)
class HelloWindowState:
    """Safe owner-window facts; no HWND value or identity data is retained."""

    valid: bool
    visible: bool
    is_foreground: bool
    enabled: bool = True
    raw_valid: bool = False
    raw_is_child: bool = False
    root_valid: bool = False
    root_owner_valid: bool = False
    foreground_valid: bool = False
    foreground_same_process: bool = False
    foreground_root_matches_owner: bool = False
    error_type: Optional[str] = None


@dataclass(frozen=True)
class DesktopOwnerResolution:
    """Internal owner handle plus redacted facts; the handle is never repr'd."""

    owner_hwnd: Optional[int] = field(repr=False)
    state: HelloWindowState


@dataclass(frozen=True)
class HelloDiagnosticEvent:
    """A redacted event suitable for a temporary probe-only timeline."""

    elapsed_ms: int
    stage: str
    hresult: Optional[int] = None
    async_status: Optional[int] = None


@dataclass(frozen=True)
class NativeHelloOutcome:
    """Low-level operation result. It deliberately contains no UI message."""

    operation: str
    status: str
    enum_value: Optional[int] = None
    hresult: Optional[int] = None
    error_type: Optional[str] = None
    ui_requested: bool = False
    interop_verified: bool = False
    local_timeout: bool = False
    async_status_before_cancel: Optional[int] = None
    async_status_after_cancel: Optional[int] = None
    # The native finally block appends release events after constructing an
    # outcome, so this low-level value deliberately shares its local timeline.
    timeline: list[HelloDiagnosticEvent] = field(default_factory=list)


@dataclass(frozen=True)
class HelloConsentResult:
    """Public, redacted consent result for a future application-level caller."""

    operation: str
    status: str
    hresult: Optional[int] = None
    error_code: Optional[int] = None
    error_type: Optional[str] = None
    ui_requested: bool = False
    interop_verified: bool = False
    local_timeout: bool = False
    async_status_before_cancel: Optional[int] = None
    async_status_after_cancel: Optional[int] = None
    timeline: tuple[HelloDiagnosticEvent, ...] = ()


class HelloConsentAdapter(Protocol):
    def inspect_window(self, hwnd: int) -> HelloWindowState:
        ...

    def check_availability(self, hwnd: int) -> NativeHelloOutcome:
        ...

    def request_verification(self, hwnd: int, message: str) -> NativeHelloOutcome:
        ...


def _failed_hresult(value: int) -> bool:
    return bool((value & 0xFFFFFFFF) & 0x80000000)


def _valid_hwnd(hwnd: object) -> bool:
    if isinstance(hwnd, bool) or not isinstance(hwnd, int) or hwnd <= 0:
        return False
    return hwnd <= (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1


class _NativeWinRTApi:
    """Delayed ctypes bindings for documented UserConsentVerifier interop."""

    _iid_statics = GUID.from_text(IID_USER_CONSENT_VERIFIER_STATICS)
    _iid_interop = GUID.from_text(IID_USER_CONSENT_VERIFIER_INTEROP)
    _iid_async_availability = GUID.from_text(IID_ASYNC_AVAILABILITY)
    _iid_async_verification = GUID.from_text(IID_ASYNC_VERIFICATION)
    _iid_async_info = GUID.from_text(IID_ASYNC_INFO)

    def __init__(self):
        combase = ctypes.WinDLL("combase.dll", use_last_error=True)
        user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        self._ro_initialize = combase.RoInitialize
        self._ro_initialize.argtypes = [ctypes.c_uint32]
        self._ro_initialize.restype = ctypes.c_int32
        self._ro_uninitialize = combase.RoUninitialize
        self._ro_uninitialize.argtypes = []
        self._ro_uninitialize.restype = None
        self._ro_get_activation_factory = combase.RoGetActivationFactory
        self._ro_get_activation_factory.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        self._ro_get_activation_factory.restype = ctypes.c_int32
        self._windows_create_string = combase.WindowsCreateString
        self._windows_create_string.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        self._windows_create_string.restype = ctypes.c_int32
        self._windows_delete_string = combase.WindowsDeleteString
        self._windows_delete_string.argtypes = [ctypes.c_void_p]
        self._windows_delete_string.restype = ctypes.c_int32
        self._is_window = user32.IsWindow
        self._is_window.argtypes = [ctypes.c_void_p]
        self._is_window.restype = ctypes.c_int
        self._is_window_visible = user32.IsWindowVisible
        self._is_window_visible.argtypes = [ctypes.c_void_p]
        self._is_window_visible.restype = ctypes.c_int
        self._is_window_enabled = user32.IsWindowEnabled
        self._is_window_enabled.argtypes = [ctypes.c_void_p]
        self._is_window_enabled.restype = ctypes.c_int
        self._get_parent = user32.GetParent
        self._get_parent.argtypes = [ctypes.c_void_p]
        self._get_parent.restype = ctypes.c_void_p
        self._get_ancestor = user32.GetAncestor
        self._get_ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self._get_ancestor.restype = ctypes.c_void_p
        self._get_foreground_window = user32.GetForegroundWindow
        self._get_foreground_window.argtypes = []
        self._get_foreground_window.restype = ctypes.c_void_p
        self._get_window_thread_process_id = user32.GetWindowThreadProcessId
        self._get_window_thread_process_id.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self._get_window_thread_process_id.restype = ctypes.c_uint32
        self._get_current_process_id = kernel32.GetCurrentProcessId
        self._get_current_process_id.argtypes = []
        self._get_current_process_id.restype = ctypes.c_uint32

    @staticmethod
    def _vtable_call(pointer: ctypes.c_void_p, index: int, argtypes: tuple[object, ...], *args: object) -> int:
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        address = vtable[index]
        if not address:
            raise OSError("NullVtableEntry")
        callback = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, *argtypes)(address)
        return int(callback(pointer, *args))

    @classmethod
    def _release(cls, pointer: ctypes.c_void_p) -> None:
        if pointer and pointer.value:
            try:
                cls._vtable_call(pointer, 2, ())
            except Exception:
                pass

    @classmethod
    def _close_async(cls, pointer: ctypes.c_void_p) -> None:
        if pointer and pointer.value:
            try:
                cls._vtable_call(pointer, 10, ())
            except Exception:
                pass

    @classmethod
    def _cancel_async(cls, pointer: ctypes.c_void_p) -> None:
        if pointer and pointer.value:
            try:
                cls._vtable_call(pointer, 9, ())
            except Exception:
                pass

    def is_window(self, hwnd: int) -> bool:
        return bool(self._is_window(ctypes.c_void_p(hwnd)))

    @staticmethod
    def _handle_value(handle: object) -> int:
        if isinstance(handle, ctypes.c_void_p):
            return int(handle.value or 0)
        return int(handle or 0)

    def _window_process_id(self, hwnd: int) -> Optional[int]:
        process_id = ctypes.c_uint32()
        thread_id = int(self._get_window_thread_process_id(ctypes.c_void_p(hwnd), ctypes.byref(process_id)))
        if thread_id == 0:
            return None
        return int(process_id.value)

    def resolve_desktop_owner_hwnd(self, hwnd: int) -> DesktopOwnerResolution:
        """Resolve a caller HWND to its same-process GA_ROOT desktop owner."""

        if not _valid_hwnd(hwnd):
            return DesktopOwnerResolution(
                None,
                HelloWindowState(
                    valid=False,
                    visible=False,
                    is_foreground=False,
                    enabled=False,
                    error_type="InvalidHwnd",
                ),
            )

        raw = ctypes.c_void_p(hwnd)
        raw_valid = bool(self._is_window(raw))
        if not raw_valid:
            return DesktopOwnerResolution(
                None,
                HelloWindowState(
                    valid=False,
                    visible=False,
                    is_foreground=False,
                    enabled=False,
                    raw_valid=False,
                    error_type="InvalidHwnd",
                ),
            )

        parent = self._handle_value(self._get_parent(raw))
        root = self._handle_value(self._get_ancestor(raw, GA_ROOT))
        if not root or not self._is_window(ctypes.c_void_p(root)):
            return DesktopOwnerResolution(
                None,
                HelloWindowState(
                    valid=False,
                    visible=False,
                    is_foreground=False,
                    enabled=False,
                    raw_valid=True,
                    raw_is_child=bool(parent and root != hwnd),
                    error_type="RootAncestorUnavailable",
                ),
            )

        root_owner = self._handle_value(self._get_ancestor(ctypes.c_void_p(root), GA_ROOTOWNER))
        root_owner_valid = bool(root_owner and self._is_window(ctypes.c_void_p(root_owner)))
        owner_process_id = self._window_process_id(root)
        current_process_id = int(self._get_current_process_id())
        owner_in_current_process = owner_process_id == current_process_id
        visible = bool(self._is_window_visible(ctypes.c_void_p(root)))
        enabled = bool(self._is_window_enabled(ctypes.c_void_p(root)))

        foreground = self._handle_value(self._get_foreground_window())
        foreground_valid = bool(foreground and self._is_window(ctypes.c_void_p(foreground)))
        foreground_process_id = self._window_process_id(foreground) if foreground_valid else None
        foreground_root = self._handle_value(self._get_ancestor(ctypes.c_void_p(foreground), GA_ROOT)) if foreground_valid else 0
        foreground_same_process = bool(
            foreground_valid
            and owner_in_current_process
            and foreground_process_id == current_process_id
        )
        foreground_root_matches_owner = bool(
            foreground_same_process
            and foreground_root == root
        )
        error_type = None if owner_in_current_process else "OwnerNotInCurrentProcess"
        state = HelloWindowState(
            valid=owner_in_current_process,
            visible=visible,
            is_foreground=foreground_root_matches_owner,
            enabled=enabled,
            raw_valid=True,
            raw_is_child=bool(parent and root != hwnd),
            root_valid=True,
            root_owner_valid=root_owner_valid,
            foreground_valid=foreground_valid,
            foreground_same_process=foreground_same_process,
            foreground_root_matches_owner=foreground_root_matches_owner,
            error_type=error_type,
        )
        return DesktopOwnerResolution(root if state.valid else None, state)

    def window_state(self, hwnd: int) -> HelloWindowState:
        return self.resolve_desktop_owner_hwnd(hwnd).state

    def check_availability(self, timeout_seconds: float) -> NativeHelloOutcome:
        initialized = False
        factory = ctypes.c_void_p()
        interop = ctypes.c_void_p()
        operation = ctypes.c_void_p()
        async_info = ctypes.c_void_p()
        class_name = ctypes.c_void_p()
        interop_verified = False
        try:
            initialize_hr = int(self._ro_initialize(RO_INIT_SINGLETHREADED))
            if _failed_hresult(initialize_hr):
                return NativeHelloOutcome("availability", "interop_unavailable", hresult=initialize_hr, error_type="RoInitializeFailed")
            initialized = True

            create_hr = int(self._windows_create_string(
                RUNTIME_CLASS_USER_CONSENT_VERIFIER,
                len(RUNTIME_CLASS_USER_CONSENT_VERIFIER),
                ctypes.byref(class_name),
            ))
            if _failed_hresult(create_hr):
                return NativeHelloOutcome("availability", "interop_unavailable", hresult=create_hr, error_type="WindowsCreateStringFailed")

            factory_hr = self._ro_get_activation_factory(
                class_name,
                ctypes.byref(self._iid_statics),
                ctypes.byref(factory),
            )
            if _failed_hresult(factory_hr):
                return NativeHelloOutcome("availability", "interop_unavailable", hresult=int(factory_hr), error_type="RoGetActivationFactoryFailed")

            interop_hr = self._vtable_call(
                factory,
                0,
                (ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
                ctypes.byref(self._iid_interop),
                ctypes.byref(interop),
            )
            if _failed_hresult(interop_hr) or not interop.value:
                return NativeHelloOutcome("availability", "interop_unavailable", hresult=interop_hr, error_type="IUserConsentVerifierInteropUnavailable")
            interop_verified = True

            operation_hr = self._vtable_call(
                factory,
                6,
                (ctypes.POINTER(ctypes.c_void_p),),
                ctypes.byref(operation),
            )
            if _failed_hresult(operation_hr) or not operation.value:
                return NativeHelloOutcome("availability", "interop_error", hresult=operation_hr, error_type="CheckAvailabilityAsyncFailed")

            query_hr = self._vtable_call(
                operation,
                0,
                (ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
                ctypes.byref(self._iid_async_info),
                ctypes.byref(async_info),
            )
            if _failed_hresult(query_hr) or not async_info.value:
                return NativeHelloOutcome("availability", "interop_error", hresult=query_hr, error_type="IAsyncInfoQueryFailed")

            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                status = ctypes.c_int32()
                status_hr = self._vtable_call(async_info, 7, (ctypes.POINTER(ctypes.c_int32),), ctypes.byref(status))
                if _failed_hresult(status_hr):
                    return NativeHelloOutcome("availability", "interop_error", hresult=status_hr, error_type="AsyncStatusFailed", interop_verified=interop_verified)
                if status.value == ASYNC_STATUS_STARTED:
                    time.sleep(0.01)
                    continue
                if status.value == ASYNC_STATUS_CANCELED:
                    return NativeHelloOutcome("availability", "timeout", error_type="AsyncCanceled", interop_verified=interop_verified)
                if status.value == ASYNC_STATUS_ERROR:
                    error_hr = ctypes.c_int32()
                    get_error_hr = self._vtable_call(async_info, 8, (ctypes.POINTER(ctypes.c_int32),), ctypes.byref(error_hr))
                    return NativeHelloOutcome(
                        "availability",
                        "interop_error",
                        hresult=error_hr.value if not _failed_hresult(get_error_hr) else get_error_hr,
                        error_type="AsyncError",
                        interop_verified=interop_verified,
                    )
                if status.value != ASYNC_STATUS_COMPLETED:
                    return NativeHelloOutcome("availability", "interop_error", error_type="UnknownAsyncStatus", interop_verified=interop_verified)
                value = ctypes.c_int32()
                result_hr = self._vtable_call(operation, 8, (ctypes.POINTER(ctypes.c_int32),), ctypes.byref(value))
                if _failed_hresult(result_hr):
                    return NativeHelloOutcome("availability", "interop_error", hresult=result_hr, error_type="GetResultsFailed", interop_verified=interop_verified)
                return NativeHelloOutcome("availability", "ok", enum_value=value.value, interop_verified=interop_verified)

            self._cancel_async(async_info)
            return NativeHelloOutcome("availability", "timeout", error_type="Timeout", interop_verified=interop_verified)
        except Exception as exc:
            return NativeHelloOutcome("availability", "interop_unavailable", error_type=type(exc).__name__)
        finally:
            self._close_async(async_info)
            self._release(async_info)
            self._release(operation)
            self._release(interop)
            self._release(factory)
            if class_name.value:
                try:
                    self._windows_delete_string(class_name)
                except Exception:
                    pass
            if initialized:
                self._ro_uninitialize()

    def request_verification(self, hwnd: int, message: str, timeout_seconds: float) -> NativeHelloOutcome:
        """Invoke the verified desktop ABI once for a caller-owned live HWND."""

        started_at = time.monotonic()
        timeline: list[HelloDiagnosticEvent] = []

        def record(stage: str, *, hresult: Optional[int] = None, async_status: Optional[int] = None) -> None:
            timeline.append(
                HelloDiagnosticEvent(
                    elapsed_ms=max(0, round((time.monotonic() - started_at) * 1000)),
                    stage=stage,
                    hresult=hresult,
                    async_status=async_status,
                )
            )

        def outcome(
            status: str,
            *,
            enum_value: Optional[int] = None,
            hresult: Optional[int] = None,
            error_type: Optional[str] = None,
            ui_requested: bool = False,
            local_timeout: bool = False,
            before_cancel: Optional[int] = None,
            after_cancel: Optional[int] = None,
        ) -> NativeHelloOutcome:
            return NativeHelloOutcome(
                "verification",
                status,
                enum_value=enum_value,
                hresult=hresult,
                error_type=error_type,
                ui_requested=ui_requested,
                local_timeout=local_timeout,
                async_status_before_cancel=before_cancel,
                async_status_after_cancel=after_cancel,
                timeline=timeline,
            )

        initialized = False
        factory = ctypes.c_void_p()
        interop = ctypes.c_void_p()
        operation = ctypes.c_void_p()
        async_info = ctypes.c_void_p()
        class_name = ctypes.c_void_p()
        prompt = ctypes.c_void_p()
        ui_requested = False
        try:
            initialize_hr = int(self._ro_initialize(RO_INIT_SINGLETHREADED))
            record("ro_initialize", hresult=initialize_hr)
            if _failed_hresult(initialize_hr):
                return outcome("interop_unavailable", hresult=initialize_hr, error_type="RoInitializeFailed")
            initialized = True

            create_hr = int(self._windows_create_string(
                RUNTIME_CLASS_USER_CONSENT_VERIFIER,
                len(RUNTIME_CLASS_USER_CONSENT_VERIFIER),
                ctypes.byref(class_name),
            ))
            record("runtime_class_hstring", hresult=create_hr)
            if _failed_hresult(create_hr):
                return outcome("interop_unavailable", hresult=create_hr, error_type="WindowsCreateStringFailed")

            factory_hr = self._ro_get_activation_factory(
                class_name,
                ctypes.byref(self._iid_statics),
                ctypes.byref(factory),
            )
            record("activation_factory", hresult=int(factory_hr))
            if _failed_hresult(factory_hr):
                return outcome("interop_unavailable", hresult=int(factory_hr), error_type="RoGetActivationFactoryFailed")

            interop_hr = self._vtable_call(
                factory,
                0,
                (ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
                ctypes.byref(self._iid_interop),
                ctypes.byref(interop),
            )
            record("interop_query", hresult=interop_hr)
            if _failed_hresult(interop_hr) or not interop.value:
                return outcome("interop_unavailable", hresult=interop_hr, error_type="IUserConsentVerifierInteropUnavailable")

            prompt_hr = int(self._windows_create_string(message, len(message), ctypes.byref(prompt)))
            record("message_hstring", hresult=prompt_hr)
            if _failed_hresult(prompt_hr):
                return outcome("interop_error", hresult=prompt_hr, error_type="WindowsCreateStringFailed")

            operation_hr = self._vtable_call(
                interop,
                6,
                (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
                ctypes.c_void_p(hwnd),
                prompt,
                ctypes.byref(self._iid_async_verification),
                ctypes.byref(operation),
            )
            ui_requested = True
            record("request_return", hresult=operation_hr)
            if _failed_hresult(operation_hr) or not operation.value:
                return outcome(
                    "interop_error",
                    hresult=operation_hr,
                    error_type="RequestVerificationForWindowAsyncFailed",
                    ui_requested=ui_requested,
                )

            deadline = time.monotonic() + timeout_seconds

            query_hr = self._vtable_call(
                operation,
                0,
                (ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
                ctypes.byref(self._iid_async_info),
                ctypes.byref(async_info),
            )
            record("async_info_query", hresult=query_hr)
            if _failed_hresult(query_hr) or not async_info.value:
                return outcome("interop_error", hresult=query_hr, error_type="IAsyncInfoQueryFailed", ui_requested=ui_requested)

            while time.monotonic() < deadline:
                if not self.is_window(hwnd):
                    record("window_destroyed")
                    self._cancel_async(async_info)
                    record("cancel_requested")
                    return outcome("window_destroyed", error_type="WindowDestroyed", ui_requested=ui_requested)
                status = ctypes.c_int32()
                status_hr = self._vtable_call(async_info, 7, (ctypes.POINTER(ctypes.c_int32),), ctypes.byref(status))
                record("async_status", hresult=status_hr, async_status=status.value if not _failed_hresult(status_hr) else None)
                if _failed_hresult(status_hr):
                    return outcome("interop_error", hresult=status_hr, error_type="AsyncStatusFailed", ui_requested=ui_requested)
                if status.value == ASYNC_STATUS_STARTED:
                    time.sleep(0.01)
                    continue
                if status.value == ASYNC_STATUS_CANCELED:
                    return outcome("canceled", error_type="AsyncCanceled", ui_requested=ui_requested)
                if status.value == ASYNC_STATUS_ERROR:
                    error_hr = ctypes.c_int32()
                    get_error_hr = self._vtable_call(async_info, 8, (ctypes.POINTER(ctypes.c_int32),), ctypes.byref(error_hr))
                    record("async_error", hresult=error_hr.value if not _failed_hresult(get_error_hr) else get_error_hr)
                    return outcome(
                        "interop_error",
                        hresult=error_hr.value if not _failed_hresult(get_error_hr) else get_error_hr,
                        error_type="AsyncError",
                        ui_requested=ui_requested,
                    )
                if status.value != ASYNC_STATUS_COMPLETED:
                    return outcome("interop_error", error_type="UnknownAsyncStatus", ui_requested=ui_requested)
                value = ctypes.c_int32()
                result_hr = self._vtable_call(operation, 8, (ctypes.POINTER(ctypes.c_int32),), ctypes.byref(value))
                record("get_results", hresult=result_hr)
                if _failed_hresult(result_hr):
                    return outcome("interop_error", hresult=result_hr, error_type="GetResultsFailed", ui_requested=ui_requested)
                return outcome("ok", enum_value=value.value, ui_requested=ui_requested)

            before_cancel = None
            before_hr = None
            status = ctypes.c_int32()
            before_hr = self._vtable_call(async_info, 7, (ctypes.POINTER(ctypes.c_int32),), ctypes.byref(status))
            if not _failed_hresult(before_hr):
                before_cancel = status.value
            record("local_timeout_before_cancel", hresult=before_hr, async_status=before_cancel)
            self._cancel_async(async_info)
            record("cancel_requested")
            after_cancel = None
            settle_deadline = time.monotonic() + POST_CANCEL_SETTLE_SECONDS
            while time.monotonic() < settle_deadline:
                status = ctypes.c_int32()
                after_hr = self._vtable_call(async_info, 7, (ctypes.POINTER(ctypes.c_int32),), ctypes.byref(status))
                after_cancel = status.value if not _failed_hresult(after_hr) else None
                record("async_status_after_cancel", hresult=after_hr, async_status=after_cancel)
                if _failed_hresult(after_hr) or after_cancel != ASYNC_STATUS_STARTED:
                    break
                time.sleep(0.01)
            return outcome(
                "timeout",
                error_type="Timeout",
                ui_requested=ui_requested,
                local_timeout=True,
                before_cancel=before_cancel,
                after_cancel=after_cancel,
            )
        except Exception as exc:
            record("exception")
            return outcome("interop_error", error_type=type(exc).__name__, ui_requested=ui_requested)
        finally:
            self._close_async(async_info)
            record("async_close")
            self._release(async_info)
            self._release(operation)
            self._release(interop)
            self._release(factory)
            record("interfaces_released")
            if prompt.value:
                try:
                    self._windows_delete_string(prompt)
                except Exception:
                    pass
            if class_name.value:
                try:
                    self._windows_delete_string(class_name)
                except Exception:
                    pass
            if initialized:
                self._ro_uninitialize()


class WindowsHelloInteropAdapter:
    """Native adapter. DLL loading remains deferred until an operation is requested."""

    def __init__(
        self,
        *,
        platform_name: Optional[str] = None,
        availability_timeout_seconds: float = DEFAULT_AVAILABILITY_TIMEOUT_SECONDS,
        verification_timeout_seconds: float = DEFAULT_VERIFICATION_TIMEOUT_SECONDS,
        native_api: Optional[_NativeWinRTApi] = None,
    ):
        self._platform_name = platform_name or sys.platform
        self._availability_timeout_seconds = availability_timeout_seconds
        self._verification_timeout_seconds = verification_timeout_seconds
        self._native: Optional[_NativeWinRTApi] = native_api
        self._load_error: Optional[str] = None

    @property
    def native_loaded(self) -> bool:
        return self._native is not None

    def _load_native(self) -> Optional[_NativeWinRTApi]:
        if self._platform_name != "win32":
            return None
        if self._native is None and self._load_error is None:
            try:
                self._native = _NativeWinRTApi()
            except Exception as exc:
                self._load_error = type(exc).__name__
        return self._native

    def check_availability(self, hwnd: int) -> NativeHelloOutcome:
        if self._platform_name != "win32":
            return NativeHelloOutcome("availability", "unsupported_platform")
        if not _valid_hwnd(hwnd):
            return NativeHelloOutcome("availability", "error", error_type="InvalidHwnd")
        native = self._load_native()
        if native is None:
            return NativeHelloOutcome("availability", "interop_unavailable", error_type=self._load_error or "NativeUnavailable")
        try:
            if not native.is_window(hwnd):
                return NativeHelloOutcome("availability", "error", error_type="InvalidHwnd")
            return native.check_availability(self._availability_timeout_seconds)
        except Exception as exc:
            return NativeHelloOutcome("availability", "interop_unavailable", error_type=type(exc).__name__)

    def inspect_window(self, hwnd: int) -> HelloWindowState:
        if self._platform_name != "win32":
            return HelloWindowState(valid=False, visible=False, is_foreground=False, enabled=False, error_type="UnsupportedPlatform")
        if not _valid_hwnd(hwnd):
            return HelloWindowState(valid=False, visible=False, is_foreground=False, enabled=False, error_type="InvalidHwnd")
        native = self._load_native()
        if native is None:
            return HelloWindowState(valid=False, visible=False, is_foreground=False, enabled=False, error_type=self._load_error or "NativeUnavailable")
        try:
            return native.resolve_desktop_owner_hwnd(hwnd).state
        except Exception as exc:
            return HelloWindowState(valid=False, visible=False, is_foreground=False, enabled=False, error_type=type(exc).__name__)

    def request_verification(self, hwnd: int, message: str) -> NativeHelloOutcome:
        if self._platform_name != "win32":
            return NativeHelloOutcome("verification", "unsupported_platform")
        if not _valid_hwnd(hwnd):
            return NativeHelloOutcome("verification", "error", error_type="InvalidHwnd")
        if not isinstance(message, str) or not message or "\x00" in message:
            return NativeHelloOutcome("verification", "error", error_type="InvalidMessage")
        native = self._load_native()
        if native is None:
            return NativeHelloOutcome("verification", "interop_unavailable", error_type=self._load_error or "NativeUnavailable")
        try:
            resolution = native.resolve_desktop_owner_hwnd(hwnd)
            owner = resolution.state
            if not owner.raw_valid or not owner.valid or resolution.owner_hwnd is None:
                return NativeHelloOutcome("verification", "error", error_type=owner.error_type or "InvalidHwnd")
            if not owner.visible:
                return NativeHelloOutcome("verification", "error", error_type="OwnerNotVisible")
            if not owner.enabled:
                return NativeHelloOutcome("verification", "error", error_type="OwnerNotEnabled")
            if not (
                owner.foreground_valid
                and owner.foreground_same_process
                and owner.foreground_root_matches_owner
            ):
                return NativeHelloOutcome("verification", "error", error_type="OwnerWindowNotActive")
            return native.request_verification(resolution.owner_hwnd, message, self._verification_timeout_seconds)
        except Exception as exc:
            return NativeHelloOutcome("verification", "interop_error", error_type=type(exc).__name__)


class WindowsHelloConsentBackend:
    """Maps raw outcomes without retaining messages, keys, or identity data."""

    def __init__(self, *, adapter: Optional[HelloConsentAdapter] = None, platform_name: Optional[str] = None):
        self._platform_name = platform_name or sys.platform
        self._adapter = adapter or WindowsHelloInteropAdapter(platform_name=self._platform_name)

    def check_availability(self, hwnd: object) -> HelloConsentResult:
        if self._platform_name != "win32":
            return HelloConsentResult("availability", "unsupported_platform")
        if not _valid_hwnd(hwnd):
            return HelloConsentResult("availability", "error", error_type="InvalidHwnd")
        try:
            outcome = self._adapter.check_availability(hwnd)
        except Exception as exc:
            return HelloConsentResult("availability", "error", error_type=type(exc).__name__)
        return self._map_availability(outcome)

    def inspect_window(self, hwnd: object) -> HelloWindowState:
        if self._platform_name != "win32":
            return HelloWindowState(valid=False, visible=False, is_foreground=False, enabled=False, error_type="UnsupportedPlatform")
        if not _valid_hwnd(hwnd):
            return HelloWindowState(valid=False, visible=False, is_foreground=False, enabled=False, error_type="InvalidHwnd")
        try:
            return self._adapter.inspect_window(hwnd)
        except Exception as exc:
            return HelloWindowState(valid=False, visible=False, is_foreground=False, enabled=False, error_type=type(exc).__name__)

    def request_verification(self, hwnd: object, message: object) -> HelloConsentResult:
        if self._platform_name != "win32":
            return HelloConsentResult("verification", "unsupported_platform")
        if not _valid_hwnd(hwnd):
            return HelloConsentResult("verification", "error", error_type="InvalidHwnd")
        if not isinstance(message, str) or not message:
            return HelloConsentResult("verification", "error", error_type="InvalidMessage")
        try:
            outcome = self._adapter.request_verification(hwnd, message)
        except Exception as exc:
            return HelloConsentResult("verification", "error", error_type=type(exc).__name__)
        return self._map_verification(outcome)

    @staticmethod
    def _map_availability(outcome: NativeHelloOutcome) -> HelloConsentResult:
        if outcome.status == "ok":
            status = AVAILABILITY_STATUS.get(outcome.enum_value)
            if status is None:
                return HelloConsentResult("availability", "unavailable", hresult=outcome.hresult, error_type="UnknownAvailabilityValue")
            return HelloConsentResult("availability", status, hresult=outcome.hresult, interop_verified=outcome.interop_verified)
        if outcome.status in {"unsupported_platform", "interop_unavailable"}:
            return HelloConsentResult("availability", outcome.status, hresult=outcome.hresult, error_type=outcome.error_type)
        if outcome.status == "timeout":
            return HelloConsentResult("availability", "unavailable", hresult=outcome.hresult, error_type="Timeout")
        return HelloConsentResult("availability", "error", hresult=outcome.hresult, error_type=outcome.error_type or "InteropError")

    @staticmethod
    def _map_verification(outcome: NativeHelloOutcome) -> HelloConsentResult:
        details = {
            "hresult": outcome.hresult,
            "ui_requested": outcome.ui_requested,
            "local_timeout": outcome.local_timeout,
            "async_status_before_cancel": outcome.async_status_before_cancel,
            "async_status_after_cancel": outcome.async_status_after_cancel,
            "timeline": tuple(outcome.timeline),
        }
        if outcome.status == "ok":
            status = VERIFICATION_STATUS.get(outcome.enum_value)
            if status is None:
                return HelloConsentResult("verification", "error", error_type="UnknownVerificationValue", **details)
            return HelloConsentResult("verification", status, **details)
        if outcome.status == "unsupported_platform":
            return HelloConsentResult("verification", "unsupported_platform", error_type=outcome.error_type, **details)
        if outcome.status == "interop_unavailable":
            return HelloConsentResult("verification", "interop_error", error_type="InteropUnavailable", **details)
        if outcome.status == "timeout":
            return HelloConsentResult("verification", "timeout", error_type="Timeout", **details)
        if outcome.status == "window_destroyed":
            return HelloConsentResult("verification", "window_destroyed", error_type="WindowDestroyed", **details)
        if outcome.status == "canceled":
            return HelloConsentResult("verification", "canceled", error_type=outcome.error_type, **details)
        return HelloConsentResult("verification", "error", error_type=outcome.error_type or "InteropError", **details)


__all__ = [
    "AVAILABILITY_STATUS",
    "DEFAULT_AVAILABILITY_TIMEOUT_SECONDS",
    "DEFAULT_VERIFICATION_TIMEOUT_SECONDS",
    "DesktopOwnerResolution",
    "GA_ROOT",
    "GA_ROOTOWNER",
    "HelloDiagnosticEvent",
    "HelloConsentResult",
    "HelloWindowState",
    "IID_ASYNC_AVAILABILITY",
    "IID_ASYNC_INFO",
    "IID_ASYNC_VERIFICATION",
    "IID_USER_CONSENT_VERIFIER_INTEROP",
    "IID_USER_CONSENT_VERIFIER_STATICS",
    "NativeHelloOutcome",
    "RUNTIME_CLASS_USER_CONSENT_VERIFIER",
    "VERIFICATION_STATUS",
    "WindowsHelloConsentBackend",
    "WindowsHelloInteropAdapter",
]
