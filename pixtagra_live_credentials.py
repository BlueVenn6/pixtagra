"""Tk-safe bridge for the injected PixTagra credential policy.

This module owns no key storage and creates no native backend.  It separates a
potentially long Windows Hello verification wait from the Tk UI thread, while
leaving all policy and credential-manager decisions in the coordinator.
"""

from __future__ import annotations

import threading
import queue
from typing import Any, Callable, Optional

from pixtagra_credential_policy import CredentialConsentChallenge, CredentialPolicyResult


class CredentialConsentBridge:
    """Serialize user-initiated consent work and return it to Tk via ``after``."""

    def __init__(self, root: Any, coordinator: Any):
        self._root = root
        self._coordinator = coordinator
        self._lock = threading.RLock()
        self._active: Optional[CredentialConsentChallenge] = None
        self._closed = False
        self._completion_queue: "queue.Queue[tuple[Callable[[], None], None]]" = queue.Queue()
        self._poll_job = None
        self._schedule_poll()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active is not None

    def start(
        self,
        operation: str,
        provider_id: Optional[str],
        hwnd: int,
        *,
        secret_supplier: Optional[Callable[[], object]] = None,
        on_complete: Callable[[CredentialPolicyResult], None],
    ) -> CredentialPolicyResult:
        """Begin a UI operation; only the verification wait leaves the UI thread."""
        with self._lock:
            if self._closed:
                result = CredentialPolicyResult(operation="credential_bridge", status="bridge_closed")
                self._schedule_ui(lambda: on_complete(result))
                return result
            if self._active is not None:
                result = CredentialPolicyResult(operation="credential_bridge", status="consent_busy", retryable=True)
                self._schedule_ui(lambda: on_complete(result))
                return result
        result = self._coordinator.begin_interactive_operation(operation, provider_id, hwnd)
        challenge = result.challenge
        if result.status != "verification_required" or challenge is None:
            self._schedule_ui(lambda: on_complete(result))
            return result
        with self._lock:
            if self._closed:
                self._coordinator.cancel_interactive_operation(challenge)
                closed = CredentialPolicyResult(operation=result.operation, status="bridge_closed")
                self._schedule_ui(lambda: on_complete(closed))
                return closed
            self._active = challenge

        def worker() -> None:
            verification = self._coordinator.request_interactive_verification(challenge)

            def finish() -> None:
                with self._lock:
                    still_active = not self._closed and self._active == challenge
                    self._active = None
                if not still_active:
                    self._coordinator.cancel_interactive_operation(challenge)
                    on_complete(CredentialPolicyResult(operation=result.operation, status="bridge_closed"))
                    return
                secret = None
                if getattr(verification, "status", None) == "verified" and secret_supplier is not None:
                    try:
                        secret = secret_supplier()
                    except Exception:
                        secret = None
                final = self._coordinator.complete_interactive_operation(challenge, verification, secret)
                on_complete(final)

            # A worker never calls Tk. The UI-thread poller below receives it.
            self._completion_queue.put((finish, None))

        threading.Thread(target=worker, name="PixTagraCredentialConsent", daemon=True).start()
        return result

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            challenge = self._active
            self._active = None
            poll_job = self._poll_job
            self._poll_job = None
        if challenge is not None:
            self._coordinator.cancel_interactive_operation(challenge)
        if poll_job is not None:
            try:
                self._root.after_cancel(poll_job)
            except Exception:
                pass

    def _schedule_ui(self, callback: Callable[[], None]) -> None:
        try:
            self._root.after(0, callback)
        except Exception:
            # Tk is already gone. The callback cannot safely touch GUI state.
            pass

    def _schedule_poll(self) -> None:
        try:
            self._poll_job = self._root.after(25, self._drain_completions)
        except Exception:
            self._poll_job = None

    def _drain_completions(self) -> None:
        with self._lock:
            if self._closed:
                return
        while True:
            try:
                callback, _unused = self._completion_queue.get_nowait()
            except queue.Empty:
                break
            callback()
        self._schedule_poll()


__all__ = ["CredentialConsentBridge"]
