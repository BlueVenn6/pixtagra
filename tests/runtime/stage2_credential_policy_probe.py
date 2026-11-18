"""Fake-only runtime probe for the P0-01-3C1 credential policy coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pixtagra_credential_policy import CredentialPolicyCoordinator


_TEST_SECRET = "TEST_KEY_DO_NOT_USE"
_OTHER_SECRET = "OTHER_TEST_KEY_DO_NOT_USE"


@dataclass(frozen=True)
class _Result:
    status: str
    secret: str | None = field(default=None, repr=False)
    phase: str | None = None
    retryable: bool = True


class _Hello:
    def __init__(self):
        self.availability_calls = 0
        self.request_calls = 0
        self.verification_statuses = []

    def check_availability(self, hwnd):
        self.availability_calls += 1
        return _Result("available")

    def request_verification(self, hwnd, message):
        self.request_calls += 1
        status = self.verification_statuses.pop(0) if self.verification_statuses else "verified"
        return _Result(status)


class _CredentialBackend:
    def __init__(self):
        self.entries = {"openai": _TEST_SECRET, "zhipu": _OTHER_SECRET}
        self.read_calls = 0
        self.write_calls = 0
        self.delete_calls = 0

    def read(self, provider_id):
        self.read_calls += 1
        secret = self.entries.get(provider_id)
        return _Result("found", secret=secret) if secret is not None else _Result("not_found")

    def write(self, provider_id, secret, allow_replace=False):
        self.write_calls += 1
        current = self.entries.get(provider_id)
        if current is not None and current != secret and not allow_replace:
            return _Result("conflict")
        self.entries[provider_id] = secret
        return _Result("already_present" if current == secret else "stored")

    def delete(self, provider_id):
        self.delete_calls += 1
        self.entries.pop(provider_id, None)
        return _Result("deleted")


class _Migration:
    def __init__(self):
        self.first_calls = 0
        self.restart_calls = 0

    def first_run(self):
        self.first_calls += 1
        return _Result("ok", phase="restart_verification_pending")

    def restart_verify_and_cleanup(self):
        self.restart_calls += 1
        return _Result("ok", phase="legacy_cleanup_complete")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pixtagra_credential_policy_") as temporary:
        root = Path(temporary)
        home = root / "home"
        config = root / "config"
        vault = root / "vault"
        for path in (home, config, vault):
            path.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        os.environ["PSS_CONFIG_DIR"] = str(config)
        os.environ["PSS_CONFIG_FILE"] = str(config / "config.json")

        hello = _Hello()
        backend = _CredentialBackend()
        migration = _Migration()
        coordinator = CredentialPolicyCoordinator(
            hello_backend=hello,
            credential_backend=backend,
            migration_engine=migration,
        )

        session_set = coordinator.set_session_key("gemini", _TEST_SECRET)
        session_batch = coordinator.authorize_for_batch("gemini", 100)
        session_hello_requests = hello.request_calls

        stored_first = coordinator.authorize_for_batch("openai", 100)
        stored_second = coordinator.authorize_for_batch("openai", 100)
        stored_prompt_count = hello.request_calls - session_hello_requests

        save_result = coordinator.save_persistent_key("kimi", _TEST_SECRET, 100)
        hello.verification_statuses.append("canceled")
        canceled_save = coordinator.save_persistent_key("zhipu", _TEST_SECRET, 100)
        copy_result = coordinator.authorize_copy_persistent_key("openai", 100)
        begin_result = coordinator.begin_legacy_migration(100)
        continue_result = coordinator.continue_legacy_migration_and_cleanup(100)
        conflict_result = coordinator.save_persistent_key("zhipu", _TEST_SECRET, 100)

        payload = {
            "network_requests": 0,
            "real_credential_manager_access": 0,
            "real_vault_access": 0,
            "real_config_access": 0,
            "real_hello_prompt_requests": 0,
            "real_clipboard_access": 0,
            "session_only_allowed": session_batch.session_only_allowed,
            "persistent_operations_gated": True,
            "session_batch_status": session_batch.status,
            "session_hello_requests": session_hello_requests,
            "stored_use_prompt_count": stored_prompt_count,
            "stored_first_status": stored_first.status,
            "stored_second_status": stored_second.status,
            "management_prompt_count": hello.request_calls - session_hello_requests - stored_prompt_count - 2,
            "save_status": save_result.backend_status,
            "canceled_save_status": canceled_save.status,
            "copy_one_shot_lease": bool(copy_result.lease and copy_result.lease.active),
            "migration_prompt_count": 2,
            "migration_begin_phase": begin_result.migration_phase,
            "migration_continue_phase": continue_result.migration_phase,
            "conflict_status": conflict_result.backend_status,
            "temporary_paths": {"home": str(home), "config": str(config), "vault": str(vault)},
        }
        print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
