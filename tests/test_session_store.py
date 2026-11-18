import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from pixtagra_session import SessionStore


def test_abrupt_process_exit_retains_paid_checkpoint_and_protected_edit(tmp_path):
    database = tmp_path / "workspace.sqlite3"
    script = '''
import os, sys
from pixtagra_session import SessionStore
store = SessionStore(sys.argv[1])
store.save([{"path":"picture.png", "identity":[123,456], "selected":False,
    "manual_fields":["title"], "data":{"title":"Manually edited title"},
    "history":[{"data":{"title":"Old title"},"manual_fields":["title"]}],
    "api_key":"secret-must-not-be-stored"}], {"mode":"Photography", "api_key":"secret-must-not-be-stored"})
store.checkpoint("picture.png", {"title":"Generated title", "keywords":["mask"],
    "api_key":"secret-must-not-be-stored"}, "review", "test-model", [123,456])
os._exit(0)
'''
    subprocess.run([sys.executable, "-c", script, str(database)], check=True)
    store = SessionStore(database)
    snapshot, checkpoints = store.load()
    assert snapshot["records"][0]["data"]["title"] == "Manually edited title"
    assert snapshot["records"][0]["manual_fields"] == ["title"]
    assert not snapshot["records"][0]["selected"]
    assert checkpoints["picture.png"]["stage"] == "review"
    assert checkpoints["picture.png"]["data"]["keywords"] == ["mask"]
    assert b"secret-must-not-be-stored" not in database.read_bytes()
    store.save([], {})
    assert store.load() == ({"records": [], "context": {}}, {})
    store.close()


def test_second_instance_cannot_overwrite_active_workspace(tmp_path):
    store = SessionStore(tmp_path / "workspace.sqlite3")
    with pytest.raises(OSError):
        SessionStore(store.path)
    store.close()
    replacement = SessionStore(store.path)
    replacement.close()


def test_failed_transaction_preserves_previous_session(tmp_path):
    store = SessionStore(tmp_path / "workspace.sqlite3")
    store.save([{"path": "original.png", "data": {"title": "Original"}}], {})
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER fail_write BEFORE INSERT ON workspace BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.save([], {})
    assert store.load()[0]["records"][0]["path"] == "original.png"
    store.close()


def test_unknown_schema_does_not_erase_session(tmp_path):
    path = tmp_path / "workspace.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        SessionStore(path)
    assert path.read_bytes() == before
