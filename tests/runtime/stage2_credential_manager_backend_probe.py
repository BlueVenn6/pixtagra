"""Exercise the P0-01-1 backend only through an in-memory fake adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pixtagra_credentials as credentials


TEST_KEY = "TEST_KEY_DO_NOT_USE"
OTHER_KEY = "OTHER_TEST_KEY_DO_NOT_USE"


class FakeAdapter:
    available = True

    def __init__(self):
        self.entries = {}
        self.calls = []

    def read_target(self, target):
        self.calls.append(("read", target))
        if target not in self.entries:
            return credentials.AdapterReadResult(
                status="not_found", error_code=credentials.ERROR_NOT_FOUND
            )
        return credentials.AdapterReadResult(status="found", blob=self.entries[target])

    def write_target(self, target, blob):
        self.calls.append(("write", target))
        self.entries[target] = bytes(blob)
        return credentials.AdapterOperationResult(success=True)

    def delete_target(self, target):
        self.calls.append(("delete", target))
        self.entries.pop(target, None)
        return credentials.AdapterOperationResult(success=True)


def main():
    with tempfile.TemporaryDirectory(prefix="pixtagra_credential_backend_") as root_text:
        root = Path(root_text)
        home = root / "home"
        config = root / "config"
        vault = home / ".pictureseo"
        for path in (home, config, vault):
            path.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        os.environ["PSS_CONFIG_DIR"] = str(config)
        os.environ["PSS_CONFIG_FILE"] = str(config / "config.json")

        adapter = FakeAdapter()
        backend = credentials.WindowsCredentialManagerBackend(adapter=adapter)
        created = backend.write("gemini", TEST_KEY)
        readback = backend.read("gemini")
        conflict = backend.write("gemini", OTHER_KEY)
        replaced = backend.write("gemini", OTHER_KEY, allow_replace=True)
        deleted = backend.delete("gemini")
        absent = backend.read("gemini")

        assert created.status == "stored"
        assert readback.status == "found" and readback.secret == TEST_KEY
        assert conflict.status == "conflict"
        assert replaced.status == "stored"
        assert deleted.status == "deleted"
        assert absent.status == "not_found"
        assert TEST_KEY not in repr(readback)
        assert TEST_KEY not in repr(adapter)

        print(json.dumps({
            "network_requests": 0,
            "real_credential_manager_access": 0,
            "temporary_paths": {
                "home": str(home),
                "config": str(config),
                "vault": str(vault),
            },
            "operations": {
                "write": created.status,
                "read": readback.status,
                "conflict": conflict.status,
                "replace": replaced.status,
                "delete": deleted.status,
                "post_delete_read": absent.status,
            },
        }, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
