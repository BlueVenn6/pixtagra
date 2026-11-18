"""Local, transactional workspace recovery. Credentials and image bytes never enter it."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading


def image_identity(path):
    try:
        stat = Path(path).stat()
        return [stat.st_size, stat.st_mtime_ns]
    except OSError:
        return None


def metadata_only(data):
    return {key: data[key] for key in (
        "title", "description", "keywords", "compliance", "keyword_review", "metadata_language", "keyword_language", "keyword_policy_version"
    ) if key in data}


class SessionStore:
    """One application owns the workspace; worker checkpoints use separate transactions."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._owner = open(self.path.with_suffix(".lock"), "a+b")
        try:
            self._owner.seek(0)
            if os.name == "nt":
                import msvcrt
                if self.path.with_suffix(".lock").stat().st_size == 0:
                    self._owner.write(b"0")
                    self._owner.flush()
                self._owner.seek(0)
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self._db() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise ValueError(f"Unsupported session version: {version}")
                db.execute("CREATE TABLE IF NOT EXISTS workspace (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS checkpoints (path TEXT PRIMARY KEY, data TEXT NOT NULL)")
                db.execute("PRAGMA user_version=1")
        except Exception:
            self._owner.close()
            raise
        self._last_snapshot = None

    @contextmanager
    def _db(self):
        with self._mutex:
            db = sqlite3.connect(self.path, timeout=5)
            try:
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    yield db
            finally:
                db.close()

    def close(self):
        self._owner.close()

    def save(self, records, context):
        # Explicit allowlists prevent accidental persistence of provider settings/keys.
        safe_records = []
        for record in records:
            safe = {key: record[key] for key in (
                "path", "identity", "selected", "saved", "processing_failed",
                "manual_fields", "last_generated_at", "last_generated_model", "regen_scope"
            ) if key in record}
            safe["data"] = metadata_only(record.get("data", {}))
            safe["history"] = [
                {"data": metadata_only(entry.get("data", {})), "time": entry.get("time", ""),
                 "model": entry.get("model", ""), "manual_fields": list(entry.get("manual_fields", []))}
                for entry in record.get("history", [])[-5:]
            ]
            safe_records.append(safe)
        safe_context = {key: context[key] for key in (
            "mode", "model", "shoot_location", "shoot_date", "visual_keyword_review"
        ) if key in context}
        payload = json.dumps({"records": safe_records, "context": safe_context}, ensure_ascii=False)
        if payload == self._last_snapshot:
            return
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO workspace VALUES (1, ?)", (payload,))
            # Removal and Clear must not resurrect old paid results on next startup.
            paths = {record["path"] for record in safe_records}
            for (path,) in db.execute("SELECT path FROM checkpoints").fetchall():
                if path not in paths:
                    db.execute("DELETE FROM checkpoints WHERE path=?", (path,))
        self._last_snapshot = payload

    def checkpoint(self, path, data, stage, model, identity):
        payload = json.dumps({"data": metadata_only(data), "stage": stage,
                              "model": model, "identity": identity}, ensure_ascii=False)
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?)", (path, payload))

    def acknowledge(self, paths):
        # Called ONLY after the merged UI state is durably committed.
        with self._db() as db:
            db.executemany("DELETE FROM checkpoints WHERE path=?", ((path,) for path in paths))

    def load(self):
        with self._db() as db:
            row = db.execute("SELECT data FROM workspace WHERE id=1").fetchone()
            checkpoints = {path: json.loads(data) for path, data in db.execute("SELECT path, data FROM checkpoints")}
        snapshot = json.loads(row[0]) if row else {"records": [], "context": {}}
        return snapshot, checkpoints
