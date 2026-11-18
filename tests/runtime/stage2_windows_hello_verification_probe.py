"""Manual-only Windows Hello verification probe with an explicit double opt-in."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from typing import Callable, Optional


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pixtagra_windows_hello import HelloConsentResult, HelloWindowState, WindowsHelloConsentBackend


VERIFICATION_MESSAGE = "PixTagra is verifying your identity for a credential security test."


def _arguments(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual-only Windows Hello verification probe.")
    parser.add_argument("--request-verification", action="store_true")
    parser.add_argument("--acknowledge-visible-prompt", action="store_true")
    return parser.parse_args(argv)


def _empty_report() -> dict[str, object]:
    return {
        "availability_status": "not_checked",
        "async_status_after_cancel": None,
        "async_status_before_cancel": None,
        "diagnostic_event_count": 0,
        "error_type": None,
        "hello_prompt_requested": 0,
        "hwnd_created": False,
        "hwnd_is_foreground": False,
        "hwnd_valid": False,
        "hwnd_visible": False,
        "interop_status": "not_checked",
        "local_timeout": False,
        "network_requests": 0,
        "platform": sys.platform,
        "real_credential_manager_access": 0,
        "real_config_access": 0,
        "real_vault_access": 0,
        "raw_hwnd_is_child": False,
        "raw_hwnd_valid": False,
        "root_hwnd_valid": False,
        "root_owner_valid": False,
        "owner_hwnd_valid": False,
        "owner_visible": False,
        "owner_enabled": False,
        "foreground_valid": False,
        "foreground_same_process": False,
        "foreground_root_matches_owner": False,
        "verification_status": "not_requested",
    }


def _verification_worker(
    backend_factory: Callable[[], WindowsHelloConsentBackend],
    hwnd: int,
    results: queue.Queue[dict[str, object]],
) -> None:
    """Run WinRT work away from Tk's event loop and return only redacted data."""

    try:
        backend = backend_factory()
        availability = backend.check_availability(hwnd)
        if availability.status == "available":
            verification = backend.request_verification(hwnd, VERIFICATION_MESSAGE)
        else:
            verification = HelloConsentResult("verification", "not_requested")
        results.put({"availability": availability, "verification": verification})
    except Exception as exc:
        results.put({"error_type": type(exc).__name__})


def _apply_results(report: dict[str, object], payload: dict[str, object]) -> None:
    if "error_type" in payload:
        report["verification_status"] = "error"
        report["error_type"] = payload["error_type"]
        return

    availability = payload["availability"]
    verification = payload["verification"]
    assert isinstance(availability, HelloConsentResult)
    assert isinstance(verification, HelloConsentResult)
    report["availability_status"] = availability.status
    report["interop_status"] = "verified" if availability.interop_verified else availability.status
    report["verification_status"] = verification.status
    report["hello_prompt_requested"] = 1 if verification.ui_requested else 0
    report["local_timeout"] = verification.local_timeout
    report["async_status_before_cancel"] = verification.async_status_before_cancel
    report["async_status_after_cancel"] = verification.async_status_after_cancel
    report["diagnostic_event_count"] = len(verification.timeline)
    report["error_type"] = verification.error_type or availability.error_type


def _record_owner_state(report: dict[str, object], owner: HelloWindowState) -> None:
    """Copy only boolean ownership facts into the public, redacted report."""

    report["raw_hwnd_valid"] = owner.raw_valid
    report["raw_hwnd_is_child"] = owner.raw_is_child
    report["root_hwnd_valid"] = owner.root_valid
    report["root_owner_valid"] = owner.root_owner_valid
    report["owner_hwnd_valid"] = owner.valid
    report["owner_visible"] = owner.visible
    report["owner_enabled"] = owner.enabled
    report["foreground_valid"] = owner.foreground_valid
    report["foreground_same_process"] = owner.foreground_same_process
    report["foreground_root_matches_owner"] = owner.foreground_root_matches_owner
    # Preserve the original report names without exposing an HWND value.
    report["hwnd_valid"] = owner.valid
    report["hwnd_visible"] = owner.visible
    report["hwnd_is_foreground"] = owner.is_foreground


def _owner_is_active(owner: HelloWindowState) -> bool:
    return bool(
        owner.raw_valid
        and owner.valid
        and owner.root_valid
        and owner.visible
        and owner.enabled
        and owner.foreground_valid
        and owner.foreground_same_process
        and owner.foreground_root_matches_owner
    )


def _write_timeline(path: Path, result: HelloConsentResult) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "async_status": event.async_status,
                    "elapsed_ms": event.elapsed_ms,
                    "hresult": event.hresult,
                    "stage": event.stage,
                }
                for event in result.timeline
            ],
            ensure_ascii=True,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def run_interactive_probe(
    *,
    backend_factory: Callable[[], WindowsHelloConsentBackend] = WindowsHelloConsentBackend,
    tk_module=None,
    thread_factory: Callable[..., threading.Thread] = threading.Thread,
    timeline_path: Optional[Path] = None,
    activation_timeout_ms: int = 500,
    activation_interval_ms: int = 50,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Show a normal owner window; only its button starts one verification."""

    report = _empty_report()
    if tk_module is None:
        import tkinter as tk_module

    root = None
    worker: Optional[threading.Thread] = None
    results: queue.Queue[dict[str, object]] = queue.Queue()
    button_clicked = False
    request_started = False
    closing = False

    try:
        root = tk_module.Tk()
        root.title("Windows Hello Verification Test")
        root.geometry("390x150")
        root.resizable(False, False)
        label = tk_module.Label(root, text="Windows Hello Verification Test")
        label.pack(padx=18, pady=(24, 12))
        status_label = tk_module.Label(root, text="")
        status_label.pack(padx=18, pady=(0, 12))

        hwnd = int(root.winfo_id())
        report["hwnd_created"] = hwnd > 0
        if hwnd <= 0:
            report["error_type"] = "InvalidHwnd"
            return report

        owner_backend = backend_factory()

        def record_payload(payload: dict[str, object]) -> None:
            _apply_results(report, payload)
            verification = payload.get("verification")
            if isinstance(verification, HelloConsentResult) and timeline_path is not None:
                _write_timeline(timeline_path, verification)

        def finish(payload: dict[str, object]) -> None:
            record_payload(payload)
            root.after(50, root.destroy)

        def poll_results() -> None:
            try:
                payload = results.get_nowait()
            except queue.Empty:
                if not closing:
                    root.after(25, poll_results)
                return
            finish(payload)

        def start_request() -> None:
            nonlocal button_clicked, request_started, worker
            if button_clicked:
                return
            button_clicked = True
            start_button.configure(state="disabled")
            status_label.configure(text="Checking active owner window...")
            deadline = monotonic() + (activation_timeout_ms / 1000)

            def wait_for_activation() -> None:
                nonlocal request_started, worker
                if closing:
                    return
                owner = owner_backend.inspect_window(hwnd)
                _record_owner_state(report, owner)
                if _owner_is_active(owner):
                    request_started = True
                    worker = thread_factory(
                        target=_verification_worker,
                        args=(backend_factory, hwnd, results),
                        daemon=True,
                    )
                    worker.start()
                    root.after(25, poll_results)
                    return
                if monotonic() >= deadline:
                    report["verification_status"] = "not_requested"
                    report["error_type"] = owner.error_type or "OwnerWindowNotActive"
                    status_label.configure(text="Owner window is not active. No request was made.")
                    root.after(50, root.destroy)
                    return
                root.after(activation_interval_ms, wait_for_activation)

            wait_for_activation()

        def close_window() -> None:
            nonlocal closing
            closing = True
            if request_started and report["verification_status"] == "not_requested":
                report["verification_status"] = "window_destroyed"
            root.destroy()

        start_button = tk_module.Button(root, text="Start Windows Hello Test", command=start_request)
        start_button.pack(padx=18, pady=(0, 20))
        root.protocol("WM_DELETE_WINDOW", close_window)
        root.mainloop()
        if worker is not None:
            worker.join(timeout=5)
            try:
                payload = results.get_nowait()
            except queue.Empty:
                payload = None
            if payload is not None:
                record_payload(payload)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    return report


def run_probe(
    argv: Optional[list[str]] = None,
    *,
    backend_factory: Callable[[], WindowsHelloConsentBackend] = WindowsHelloConsentBackend,
    tk_module=None,
    interactive_runner: Optional[Callable[..., dict[str, object]]] = None,
    timeline_path: Optional[Path] = None,
) -> dict[str, object]:
    """Run nothing by default; only the double opt-in creates an owner window."""

    options = _arguments(argv)
    if not (options.request_verification and options.acknowledge_visible_prompt):
        return _empty_report()
    runner = interactive_runner or run_interactive_probe
    return runner(
        backend_factory=backend_factory,
        tk_module=tk_module,
        timeline_path=timeline_path,
    )


def main(argv: Optional[list[str]] = None) -> int:
    with tempfile.TemporaryDirectory(prefix="pixtagra_hello_verification_") as root_text:
        root = Path(root_text)
        home = root / "home"
        config = root / "config"
        timeline = root / "verification-timeline.json"
        home.mkdir()
        config.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        os.environ["PSS_CONFIG_DIR"] = str(config)
        os.environ["PSS_CONFIG_FILE"] = str(config / "config.json")
        print(json.dumps(run_probe(argv, timeline_path=timeline), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
