"""SQLite storage: jobs, proxied requests, web sessions and key usage."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

JSON_FIELDS = ("params", "inputs", "outputs", "result")
JOB_COLUMNS = ("id", "service", "state", "params", "inputs", "outputs", "result", "progress", "stage",
               "model", "model_state", "error", "log_tail", "client", "estimate_s", "created_at",
               "started_at", "finished_at", "elapsed_s", "outputs_purged")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, service TEXT NOT NULL, state TEXT NOT NULL,
  params TEXT, inputs TEXT, outputs TEXT, result TEXT,
  progress REAL DEFAULT 0, stage TEXT, model TEXT, model_state TEXT, error TEXT, log_tail TEXT,
  client TEXT, estimate_s REAL, created_at REAL NOT NULL, started_at REAL, finished_at REAL,
  elapsed_s REAL, outputs_purged INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT, method TEXT, path TEXT, status INTEGER,
  client TEXT, model TEXT, started_at REAL, duration_s REAL, bytes_out INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS requests_started ON requests(started_at DESC);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, client TEXT, created_at REAL, expires_at REAL, key_id TEXT
);
CREATE TABLE IF NOT EXISTS key_usage (
  key_id TEXT PRIMARY KEY, last_used_at REAL, last_ip TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL, updated_by TEXT
);
"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(sessions)")}
            if "key_id" not in columns:  # databases from before keys had ids: those sessions log in again
                self.conn.execute("ALTER TABLE sessions ADD COLUMN key_id TEXT")
        path.chmod(0o600)

    # ------------------------------------------------------------ jobs

    def save_job(self, job: dict[str, Any]) -> None:
        row = {k: job.get(k) for k in JOB_COLUMNS}
        for k in JSON_FIELDS:
            row[k] = json.dumps(row[k]) if row[k] is not None else None
        row["outputs_purged"] = int(bool(row["outputs_purged"]))
        cols = ", ".join(JOB_COLUMNS)
        marks = ", ".join("?" for _ in JOB_COLUMNS)
        with self.lock:
            self.conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({marks})",
                              [row[k] for k in JOB_COLUMNS])

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        job = dict(row)
        for k in JSON_FIELDS:
            job[k] = json.loads(job[k]) if job[k] else None
        job["outputs_purged"] = bool(job["outputs_purged"])
        return job

    def get_job(self, jid: str) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, state: Optional[str] = None, service: Optional[str] = None,
                  limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        where, args = [], []
        if state:
            where.append("state = ?")
            args.append(state)
        if service:
            where.append("service = ?")
            args.append(service)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self.lock:
            total = self.conn.execute(f"SELECT COUNT(*) FROM jobs {clause}", args).fetchone()[0]
            rows = self.conn.execute(f"SELECT * FROM jobs {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                                     [*args, limit, offset]).fetchall()
        return [self._job(r) for r in rows], total

    def delete_job(self, jid: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM jobs WHERE id = ?", (jid,))

    def jobs_in_states(self, states: tuple[str, ...]) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in states)
        with self.lock:
            rows = self.conn.execute(f"SELECT * FROM jobs WHERE state IN ({marks})", states).fetchall()
        return [self._job(r) for r in rows]

    def finished_before(self, cutoff: float, purged: Optional[bool] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ?"
        args: list[Any] = [cutoff]
        if purged is not None:
            sql += " AND outputs_purged = ?"
            args.append(int(purged))
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [self._job(r) for r in rows]

    def stats(self) -> dict[str, int]:
        with self.lock:
            rows = self.conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall()
        return {r[0]: r[1] for r in rows}

    # ------------------------------------------------------------ proxied requests

    def add_request(self, rec: dict[str, Any]) -> int:
        cols = ("service", "method", "path", "status", "client", "model", "started_at", "duration_s",
                "bytes_out", "error")
        with self.lock:
            cur = self.conn.execute(f"INSERT INTO requests ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                                    [rec.get(c) for c in cols])
            return int(cur.lastrowid)

    def list_requests(self, service: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM requests", []
        if service:
            sql += " WHERE service = ?"
            args.append(service)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def purge_requests(self, cutoff: float) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM requests WHERE started_at < ?", (cutoff,))

    # ------------------------------------------------------------ settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any, who: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?, ?)",
                              (key, json.dumps(value), time.time(), who))

    # ------------------------------------------------------------ sessions

    def add_session(self, token: str, client: str, key_id: str, days: float) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute("INSERT INTO sessions (token_hash, client, created_at, expires_at, key_id) "
                              "VALUES (?, ?, ?, ?, ?)", (_hash(token), client, now, now + days * 86400, key_id))

    def session(self, token: str) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT client, key_id, expires_at FROM sessions WHERE token_hash = ?",
                                    (_hash(token),)).fetchone()
        if not row or row["expires_at"] < time.time() or not row["key_id"]:
            return None
        return {"client": row["client"], "key_id": row["key_id"]}

    def delete_session(self, token: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
            self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))

    def delete_key_sessions(self, key_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM sessions WHERE key_id = ?", (key_id,))

    # ------------------------------------------------------------ key usage

    def touch_key(self, key_id: str, when: float, ip: Optional[str]) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO key_usage VALUES (?, ?, ?)", (key_id, when, ip))

    def key_usage(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT key_id, last_used_at, last_ip FROM key_usage").fetchall()
        return {r["key_id"]: {"last_used_at": r["last_used_at"], "last_ip": r["last_ip"]} for r in rows}
