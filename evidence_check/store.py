"""Durable state in one SQLite file: cases and verdicts, reviewer decisions, a job queue, and a hash-chained audit log.

SQLite keeps a single-machine deployment free of extra services; WAL mode lets the API and workers share it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY, domain TEXT, claimant_id TEXT, case_dir TEXT, status TEXT,
  score REAL, level TEXT, verdict TEXT, segment TEXT, created REAL, checked REAL, policy TEXT
);
CREATE INDEX IF NOT EXISTS cases_by_risk ON cases(status, score DESC);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, finding INTEGER, check_id TEXT, decision TEXT,
  reason TEXT, actor TEXT, seconds REAL, ts REAL
);
CREATE INDEX IF NOT EXISTS decisions_by_case ON decisions(case_id);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, status TEXT, attempts INTEGER DEFAULT 0, error TEXT,
  created REAL, started REAL, finished REAL
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, id);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, target TEXT, detail TEXT,
  prev TEXT, hash TEXT
);
CREATE TABLE IF NOT EXISTS daily_flags (
  day TEXT, check_id TEXT, cases INTEGER, flagged INTEGER, PRIMARY KEY (day, check_id)
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write = threading.Lock()
        with self.db as c:
            c.executescript(SCHEMA)

    @property
    def db(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
        return c

    # -- audit (tamper-evident: each row hashes the previous row's hash) --------
    def audit(self, actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
        with self._write:
            c = self.db
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
                prev = row["hash"] if row else "0" * 64
                ts, det = time.time(), json.dumps(detail or {}, sort_keys=True, default=str)
                h = hashlib.sha256(f"{prev}|{ts!r}|{actor}|{action}|{target}|{det}".encode()).hexdigest()
                c.execute("INSERT INTO audit(ts, actor, action, target, detail, prev, hash) VALUES (?,?,?,?,?,?,?)",
                          (ts, actor, action, target, det, prev, h))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def verify_audit(self) -> tuple[bool, int | None]:
        """Recompute the chain. Returns (ok, id of the first row that does not match)."""
        prev = "0" * 64
        for r in self.db.execute("SELECT * FROM audit ORDER BY id"):
            h = hashlib.sha256(f"{prev}|{r['ts']!r}|{r['actor']}|{r['action']}|{r['target']}|{r['detail']}".encode()).hexdigest()
            if r["prev"] != prev or r["hash"] != h:
                return False, r["id"]
            prev = h
        return True, None

    def audit_log(self, limit: int = 200, target: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM audit", []
        if target:
            q, args = q + " WHERE target = ?", [target]
        return [dict(r) for r in self.db.execute(q + " ORDER BY id DESC LIMIT ?", [*args, limit])]

    # -- cases -------------------------------------------------------------------
    def upsert_case(self, case_id: str, domain: str, claimant_id: str, case_dir: str, segment: dict, status: str = "queued") -> None:
        self.db.execute(
            "INSERT INTO cases(case_id, domain, claimant_id, case_dir, status, segment, created) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(case_id) DO UPDATE SET case_dir=excluded.case_dir, status=excluded.status",
            (case_id, domain, claimant_id, case_dir, status, json.dumps(segment), time.time()))

    def save_verdict(self, case_id: str, verdict: dict) -> None:
        self.db.execute("UPDATE cases SET status='checked', score=?, level=?, verdict=?, checked=?, policy=? WHERE case_id=?",
                        (verdict["score"], verdict["level"], json.dumps(verdict, default=str), time.time(), verdict.get("policy"), case_id))

    def set_status(self, case_id: str, status: str) -> None:
        self.db.execute("UPDATE cases SET status=? WHERE case_id=?", (status, case_id))

    def case(self, case_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return _case_row(r) if r else None

    def cases(self, level: str | None = None, status: str | None = None, limit: int = 500) -> list[dict]:
        q, args = "SELECT * FROM cases WHERE 1=1", []
        if level:
            q, args = q + " AND level=?", [*args, level]
        if status:
            q, args = q + " AND status=?", [*args, status]
        rows = self.db.execute(q + " ORDER BY (status='checked') DESC, score DESC, created DESC LIMIT ?", [*args, limit])
        return [_case_row(r) for r in rows]

    # -- decisions ---------------------------------------------------------------
    def decide(self, case_id: str, finding: int, check_id: str, decision: str, reason: str, actor: str, seconds: float | None) -> None:
        self.db.execute("INSERT INTO decisions(case_id, finding, check_id, decision, reason, actor, seconds, ts) VALUES (?,?,?,?,?,?,?,?)",
                        (case_id, finding, check_id, decision, reason, actor, seconds, time.time()))

    def decisions(self, case_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM decisions", []
        if case_id:
            q, args = q + " WHERE case_id=?", [case_id]
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", args)]

    def latest_decisions(self, case_id: str) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for d in self.decisions(case_id):
            out[d["finding"]] = d
        return out

    def check_precision(self) -> dict[str, dict[str, Any]]:
        """Live precision per check from reviewer decisions (latest decision per finding counts)."""
        latest: dict[tuple, dict] = {}
        for d in self.decisions():
            latest[(d["case_id"], d["finding"])] = d
        out: dict[str, dict[str, Any]] = {}
        for d in latest.values():
            s = out.setdefault(d["check_id"], {"confirmed": 0, "dismissed": 0})
            s["confirmed" if d["decision"] == "confirm" else "dismissed"] += 1
        for s in out.values():
            n = s["confirmed"] + s["dismissed"]
            s["precision"] = round(s["confirmed"] / n, 3) if n else None
        return out

    # -- job queue ---------------------------------------------------------------
    def enqueue(self, case_id: str) -> int:
        cur = self.db.execute("INSERT INTO jobs(case_id, status, created) VALUES (?, 'queued', ?)", (case_id, time.time()))
        return int(cur.lastrowid)

    def claim_job(self, max_attempts: int = 3) -> dict | None:
        """Atomically take the oldest queued job (safe with several worker processes)."""
        with self._write:
            c = self.db
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute("SELECT * FROM jobs WHERE status='queued' AND attempts < ? ORDER BY id LIMIT 1", (max_attempts,)).fetchone()
                if r:
                    c.execute("UPDATE jobs SET status='running', attempts=attempts+1, started=? WHERE id=?", (time.time(), r["id"]))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return dict(r) if r else None

    def finish_job(self, job_id: int, error: str | None = None, max_attempts: int = 3) -> None:
        if error is None:
            self.db.execute("UPDATE jobs SET status='done', finished=?, error=NULL WHERE id=?", (time.time(), job_id))
            return
        r = self.db.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        status = "failed" if r and r["attempts"] >= max_attempts else "queued"  # retried until attempts run out
        self.db.execute("UPDATE jobs SET status=?, error=?, finished=? WHERE id=?", (status, error[:500], time.time(), job_id))

    def requeue_stale(self, older_than_s: float = 900) -> int:
        """Jobs left 'running' by a crashed worker go back to the queue."""
        cur = self.db.execute("UPDATE jobs SET status='queued' WHERE status='running' AND started < ?", (time.time() - older_than_s,))
        return cur.rowcount

    def queue_stats(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}

    # -- monitoring --------------------------------------------------------------
    def record_flags(self, day: str, fired: set[str], all_checks: set[str]) -> None:
        for chk in all_checks | fired:
            self.db.execute("INSERT INTO daily_flags(day, check_id, cases, flagged) VALUES (?,?,1,?) ON CONFLICT(day, check_id) "
                            "DO UPDATE SET cases=cases+1, flagged=flagged+excluded.flagged", (day, chk, int(chk in fired)))

    def flag_series(self, check_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM daily_flags", []
        if check_id:
            q, args = q + " WHERE check_id=?", [check_id]
        return [dict(r) for r in self.db.execute(q + " ORDER BY day", args)]


def _case_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["verdict"] = json.loads(d["verdict"]) if d.get("verdict") else None
    d["segment"] = json.loads(d["segment"]) if d.get("segment") else {}
    return d
