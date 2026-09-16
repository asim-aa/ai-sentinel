"""Shared SQLite storage for spans, synthetic checks, and incidents.

One file, written by the demo service (spans, via tracing.py) and the Sentinel engine (synthetic
checks, incidents) from two separate processes. WAL mode keeps concurrent readers/writers from
blocking each other at this scale.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_id TEXT,
    name TEXT NOT NULL,
    service_name TEXT NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    duration_ms REAL NOT NULL,
    status TEXT NOT NULL,
    attributes TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_start ON spans(start_time);
CREATE INDEX IF NOT EXISTS idx_spans_name ON spans(name);

CREATE TABLE IF NOT EXISTS synthetic_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    passed INTEGER NOT NULL,
    latency_ms REAL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_ts ON synthetic_checks(ts);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    detector TEXT NOT NULL,
    severity TEXT NOT NULL,
    summary TEXT NOT NULL,
    root_cause TEXT,
    confidence REAL,
    recommended_action TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    resolved_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
"""


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- spans ----

def insert_span(
    db_path: str,
    *,
    span_id: str,
    trace_id: str,
    parent_id: str | None,
    name: str,
    service_name: str,
    start_time: float,
    end_time: float,
    duration_ms: float,
    status: str,
    attributes: dict,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO spans
               (span_id, trace_id, parent_id, name, service_name, start_time, end_time,
                duration_ms, status, attributes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                span_id, trace_id, parent_id, name, service_name, start_time, end_time,
                duration_ms, status, json.dumps(attributes, default=str),
            ),
        )


def _row_to_span(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["attributes"] = json.loads(d["attributes"]) if d["attributes"] else {}
    return d


def spans_since(db_path: str, start_ts: float, end_ts: float, name: str | None = None) -> list[dict]:
    with _connect(db_path) as conn:
        if name:
            rows = conn.execute(
                "SELECT * FROM spans WHERE start_time >= ? AND start_time <= ? AND name = ? ORDER BY start_time",
                (start_ts, end_ts, name),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM spans WHERE start_time >= ? AND start_time <= ? ORDER BY start_time",
                (start_ts, end_ts),
            ).fetchall()
        return [_row_to_span(r) for r in rows]


def recent_traces(db_path: str, limit: int = 20) -> list[dict]:
    """Most recent top-level chat_request spans, each with its child stage spans attached."""
    with _connect(db_path) as conn:
        roots = conn.execute(
            "SELECT * FROM spans WHERE name = 'chat_request' ORDER BY start_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for root in roots:
            root_d = _row_to_span(root)
            children = conn.execute(
                "SELECT * FROM spans WHERE trace_id = ? AND span_id != ? ORDER BY start_time",
                (root_d["trace_id"], root_d["span_id"]),
            ).fetchall()
            root_d["stages"] = [_row_to_span(c) for c in children]
            out.append(root_d)
        return out


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(pct / 100 * (len(s) - 1)))))
    return s[idx]


def stage_breakdown(
    db_path: str, window_s: float, stage_names: tuple[str, ...], end_ts: float | None = None
) -> dict[str, dict]:
    now = end_ts if end_ts is not None else time.time()
    out = {}
    for stage in stage_names:
        rows = spans_since(db_path, now - window_s, now, name=stage)
        durations = [r["duration_ms"] for r in rows]
        errors = [r for r in rows if r["status"] == "ERROR"]
        out[stage] = {
            "count": len(rows),
            "error_rate": (len(errors) / len(rows)) if rows else 0.0,
            "p50_ms": _percentile(durations, 50),
            "p95_ms": _percentile(durations, 95),
        }
    return out


def metrics_summary(db_path: str, window_s: float, end_ts: float | None = None) -> dict:
    now = end_ts if end_ts is not None else time.time()
    rows = spans_since(db_path, now - window_s, now, name="chat_request")
    durations = [r["duration_ms"] for r in rows]
    errors = [r for r in rows if r["status"] == "ERROR"]
    timeouts = [r for r in rows if r["attributes"].get("error_reason") == "timeout"]
    invalid = [r for r in rows if r["attributes"].get("invalid_output")]
    tokens = [r["attributes"].get("tokens_total") for r in rows if r["attributes"].get("tokens_total")]

    count = len(rows)
    return {
        "window_s": window_s,
        "count": count,
        "availability": 1 - (len(errors) / count) if count else 1.0,
        "error_rate": (len(errors) / count) if count else 0.0,
        "timeout_rate": (len(timeouts) / count) if count else 0.0,
        "invalid_output_rate": (len(invalid) / count) if count else 0.0,
        "p50_ms": _percentile(durations, 50),
        "p95_ms": _percentile(durations, 95),
        "p99_ms": _percentile(durations, 99),
        "avg_tokens": (sum(tokens) / len(tokens)) if tokens else 0.0,
    }


# -------------------------------------------------------- synthetic checks --

def insert_synthetic_check(db_path: str, *, passed: bool, latency_ms: float, detail: str = "") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO synthetic_checks (ts, passed, latency_ms, detail) VALUES (?, ?, ?, ?)",
            (time.time(), int(passed), latency_ms, detail),
        )


def last_synthetic_check(db_path: str) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM synthetic_checks ORDER BY ts DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def checks_stats(db_path: str, window_s: float, end_ts: float | None = None) -> dict:
    now = end_ts if end_ts is not None else time.time()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM synthetic_checks WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (now - window_s, now),
        ).fetchall()
    rows = [dict(r) for r in rows]
    passed = [r for r in rows if r["passed"]]
    return {
        "count": len(rows),
        "passed": len(passed),
        "fail_rate": 1 - (len(passed) / len(rows)) if rows else 0.0,
        "avg_latency_ms": (sum(r["latency_ms"] for r in rows) / len(rows)) if rows else 0.0,
    }


# -------------------------------------------------------------- incidents --

def create_incident(
    db_path: str,
    *,
    detector: str,
    severity: str,
    summary: str,
    root_cause: str,
    confidence: float,
    recommended_action: str,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO incidents
               (ts, detector, severity, summary, root_cause, confidence, recommended_action, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
            (time.time(), detector, severity, summary, root_cause, confidence, recommended_action),
        )
        return cur.lastrowid


def open_incident_for_detector(db_path: str, detector: str, cooldown_s: float = 120) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM incidents WHERE detector = ? AND status = 'open'
               AND ts >= ? ORDER BY ts DESC LIMIT 1""",
            (detector, time.time() - cooldown_s),
        ).fetchone()
        return dict(row) if row else None


def list_incidents(db_path: str, status: str | None = None, limit: int = 50) -> list[dict]:
    with _connect(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM incidents WHERE status = ? ORDER BY ts DESC LIMIT ?", (status, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM incidents ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_incident(db_path: str, incident_id: int) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None


def update_incident_status(db_path: str, incident_id: int, status: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE incidents SET status = ?, resolved_ts = ? WHERE id = ?",
            (status, time.time() if status != "open" else None, incident_id),
        )
