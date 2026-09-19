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
    resolved_ts REAL,
    stage TEXT,
    merged_detectors TEXT,
    canary_result TEXT,
    evidence TEXT,
    last_seen_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_stage ON incidents(stage);

CREATE TABLE IF NOT EXISTS remediation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    metric TEXT NOT NULL,
    before_value REAL,
    after_value REAL,
    started_at REAL NOT NULL,
    finished_at REAL,
    outcome TEXT NOT NULL DEFAULT 'verifying',
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_remediation_runs_incident ON remediation_runs(incident_id);

CREATE TABLE IF NOT EXISTS regressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detector TEXT NOT NULL,
    stage TEXT,
    fault_mode TEXT,
    summary TEXT,
    root_cause TEXT,
    action TEXT NOT NULL,
    metric TEXT NOT NULL,
    before_value REAL,
    after_value REAL,
    source_incident_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_run_at REAL,
    last_run_passed INTEGER,
    last_run_detail TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_regressions_signature ON regressions(detector, stage);

CREATE TABLE IF NOT EXISTS blind_eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    trial_count INTEGER NOT NULL,
    accuracy REAL NOT NULL,
    counts TEXT NOT NULL,
    trials TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blind_eval_runs_ts ON blind_eval_runs(ts);

CREATE TABLE IF NOT EXISTS quality_eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    judge_model TEXT NOT NULL,
    prompt_count INTEGER NOT NULL,
    pass_count INTEGER NOT NULL,
    total_cost_usd REAL NOT NULL,
    results TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_eval_runs_ts ON quality_eval_runs(ts);
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


# Columns added to `incidents` after it may already exist on a deployed database.
# `CREATE TABLE IF NOT EXISTS` silently no-ops against an existing table, so a new column needs
# an explicit ALTER here or it never reaches a database that predates it (a fresh dev DB doesn't
# need this — it gets the column from CREATE TABLE below).
_INCIDENT_MIGRATIONS = {
    "stage": "TEXT",
    "merged_detectors": "TEXT",
    "canary_result": "TEXT",
    "evidence": "TEXT",
    "last_seen_ts": "REAL",
}


def _migrate_incidents_table(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(incidents)")}
    if not existing:
        return
    for column, col_type in _INCIDENT_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE incidents ADD COLUMN {column} {col_type}")


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        _migrate_incidents_table(conn)
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


def _drop_excluded(rows: list[dict], exclude_periods: list[tuple[float, float]] | None) -> list[dict]:
    """Drop spans that fall inside a known-anomalous period, so a baseline computation
    doesn't get pulled toward a fault it's supposed to be a clean comparison point for."""
    if not exclude_periods:
        return rows
    return [r for r in rows if not any(s <= r["start_time"] <= e for s, e in exclude_periods)]


def stage_breakdown(
    db_path: str,
    window_s: float,
    stage_names: tuple[str, ...],
    end_ts: float | None = None,
    exclude_periods: list[tuple[float, float]] | None = None,
) -> dict[str, dict]:
    now = end_ts if end_ts is not None else time.time()
    out = {}
    for stage in stage_names:
        rows = _drop_excluded(spans_since(db_path, now - window_s, now, name=stage), exclude_periods)
        durations = [r["duration_ms"] for r in rows]
        errors = [r for r in rows if r["status"] == "ERROR"]
        out[stage] = {
            "count": len(rows),
            "error_rate": (len(errors) / len(rows)) if rows else 0.0,
            "p50_ms": _percentile(durations, 50),
            "p95_ms": _percentile(durations, 95),
        }
    return out


def metrics_summary(
    db_path: str,
    window_s: float,
    end_ts: float | None = None,
    exclude_periods: list[tuple[float, float]] | None = None,
) -> dict:
    now = end_ts if end_ts is not None else time.time()
    rows = _drop_excluded(spans_since(db_path, now - window_s, now, name="chat_request"), exclude_periods)
    durations = [r["duration_ms"] for r in rows]
    errors = [r for r in rows if r["status"] == "ERROR"]
    timeouts = [r for r in rows if r["attributes"].get("error_reason") == "timeout"]
    invalid = [r for r in rows if r["attributes"].get("invalid_output")]
    tokens = [r["attributes"].get("tokens_total") for r in rows if r["attributes"].get("tokens_total")]
    costs = [r["attributes"].get("cost_usd") for r in rows if r["attributes"].get("cost_usd") is not None]

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
        "avg_cost_usd": (sum(costs) / len(costs)) if costs else 0.0,
        "total_cost_usd": sum(costs) if costs else 0.0,
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


def reliability_summary(db_path: str, window_s: float, end_ts: float | None = None) -> dict:
    """How the system has been doing lately, not just right now: incidents raised, how many
    remediations actually verified vs. had to roll back, and how long recovery took -- the "24h
    reliability card" view, same recent-window shape as metrics_summary/checks_stats above."""
    now = end_ts if end_ts is not None else time.time()
    start = now - window_s
    with _connect(db_path) as conn:
        incident_count = conn.execute(
            "SELECT COUNT(*) AS c FROM incidents WHERE ts >= ? AND ts <= ?", (start, now)
        ).fetchone()["c"]
        runs = conn.execute(
            """SELECT remediation_runs.outcome AS outcome, remediation_runs.finished_at AS finished_at,
                      incidents.ts AS incident_ts
               FROM remediation_runs JOIN incidents ON incidents.id = remediation_runs.incident_id
               WHERE remediation_runs.finished_at IS NOT NULL
                 AND remediation_runs.finished_at >= ? AND remediation_runs.finished_at <= ?""",
            (start, now),
        ).fetchall()

    runs = [dict(r) for r in runs]
    verified = [r for r in runs if r["outcome"] == "verified"]
    rolled_back = [r for r in runs if r["outcome"] == "rolled_back"]
    recovery_times = [r["finished_at"] - r["incident_ts"] for r in verified]

    return {
        "window_s": window_s,
        "incident_count": incident_count,
        "verified_count": len(verified),
        "rolled_back_count": len(rolled_back),
        "median_recovery_s": _percentile(recovery_times, 50) if recovery_times else None,
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
    ts: float | None = None,
    stage: str | None = None,
    canary_result: str | None = None,
    evidence: dict | None = None,
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO incidents
               (ts, detector, severity, summary, root_cause, confidence, recommended_action, status,
                stage, merged_detectors, canary_result, evidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (ts if ts is not None else time.time(), detector, severity, summary, root_cause,
             confidence, recommended_action, stage, detector, canary_result,
             json.dumps(evidence) if evidence is not None else None),
        )
        return cur.lastrowid


def touch_incident(db_path: str, incident_id: int, ts: float | None = None) -> None:
    """Records that an open incident's detector is still firing right now. That's the only signal
    `excluded_periods` has for how long the fault actually lasted, as opposed to how long nobody
    has gotten around to resolving the incident."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE incidents SET last_seen_ts = ? WHERE id = ? AND status = 'open'",
            (ts if ts is not None else time.time(), incident_id),
        )


def open_incident_for_stage(db_path: str, stage: str | None, cooldown_s: float = 60) -> dict | None:
    """Finds an already-open incident diagnosed to the same root-cause stage very recently, so a
    second detector firing for what's really the same underlying problem gets merged into it
    instead of spawning a separate incident an operator would have to notice are related."""
    if not stage:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM incidents WHERE stage = ? AND status = 'open'
               AND ts >= ? ORDER BY ts DESC LIMIT 1""",
            (stage, time.time() - cooldown_s),
        ).fetchone()
        return dict(row) if row else None


def merge_detector_into_incident(db_path: str, incident_id: int, detector: str) -> None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT merged_detectors FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        existing = [d for d in (row["merged_detectors"] or "").split(",") if d] if row else []
        if detector not in existing:
            existing.append(detector)
        conn.execute(
            "UPDATE incidents SET merged_detectors = ? WHERE id = ?",
            (",".join(existing), incident_id),
        )


def excluded_periods(
    db_path: str, lookback_s: float, detector: str, anomaly_lead_s: float
) -> list[tuple[float, float]]:
    """Conservative time ranges to treat as known-anomalous for this detector, so a baseline
    computation can exclude them instead of being pulled toward a fault that's still ongoing
    (or only recently stopped). Each incident's range starts `anomaly_lead_s` before it was
    created -- roughly the trigger window that led to it.

    Where it ends depends on what's known about the fault. A resolved incident ends at
    resolution. An open one ends where its detector was last seen firing (`last_seen_ts`, kept
    current by `touch_incident` on every sweep that re-fires it) -- so once the fault stops and
    the detector goes quiet, the range stops growing and the clean traffic after it counts toward
    the baseline again. Ending an open incident at "now" instead (the original behavior) also
    excluded all the clean traffic after the fault, and once the incident aged past ~12 minutes
    that emptied the baseline entirely, blinding the detector to any later fault until someone
    resolved it -- found live by running the blind eval at scale, twice: first as a permanent
    blackout, then, after capping it, as an ~8-minute blind spot per incident.

    An open incident with no `last_seen_ts` (never re-fired, or predating the column) has no
    evidence about the fault's extent, so it falls back to the conservative bound: now, capped at
    `lookback_s` past its own start so it can't grow forever."""
    now = time.time()
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT ts, resolved_ts, status, last_seen_ts FROM incidents
               WHERE detector = ? AND (status = 'open' OR resolved_ts >= ?)""",
            (detector, now - lookback_s),
        ).fetchall()

    def end_of(r) -> float:
        if r["status"] != "open":
            return r["resolved_ts"]
        if r["last_seen_ts"] is not None:
            return r["last_seen_ts"]
        return min(now, r["ts"] + lookback_s)

    return [(r["ts"] - anomaly_lead_s, end_of(r)) for r in rows]


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


# ------------------------------------------------------------ remediation runs --

def create_remediation_run(
    db_path: str, *, incident_id: int, action: str, metric: str, before_value: float, started_at: float
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO remediation_runs
               (incident_id, action, metric, before_value, started_at, outcome)
               VALUES (?, ?, ?, ?, ?, 'verifying')""",
            (incident_id, action, metric, before_value, started_at),
        )
        return cur.lastrowid


def finish_remediation_run(
    db_path: str, run_id: int, *, after_value: float, outcome: str, detail: str,
    finished_at: float | None = None,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE remediation_runs SET after_value = ?, outcome = ?, detail = ?, finished_at = ? WHERE id = ?",
            (after_value, outcome, detail, finished_at if finished_at is not None else time.time(), run_id),
        )


def latest_remediation_run(db_path: str, incident_id: int) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM remediation_runs WHERE incident_id = ? ORDER BY id DESC LIMIT 1",
            (incident_id,),
        ).fetchone()
        return dict(row) if row else None


def list_remediation_runs(db_path: str, limit: int = 50) -> list[dict]:
    """Every remediation ever run, most recent first, each carrying enough of its parent
    incident's context (detector, summary, stage) to read as a standalone audit entry — every
    action here was a human clicking a button, never triggered automatically; that's an
    architectural invariant of this system, not a per-row fact worth storing."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT remediation_runs.*, incidents.detector AS incident_detector,
                      incidents.summary AS incident_summary, incidents.stage AS incident_stage
               FROM remediation_runs
               JOIN incidents ON incidents.id = remediation_runs.incident_id
               ORDER BY remediation_runs.started_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# -------------------------------------------------------------- regressions --

def upsert_regression(
    db_path: str,
    *,
    detector: str,
    stage: str | None,
    fault_mode: str | None,
    summary: str | None,
    root_cause: str | None,
    action: str,
    metric: str,
    before_value: float | None,
    after_value: float | None,
    source_incident_id: int,
) -> int:
    """One row per (detector, stage) signature — a later verified fix for the same signature
    replaces the earlier one, so the corpus reflects the current best-known fix rather than
    growing forever on a long-running deployment."""
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO regressions
               (detector, stage, fault_mode, summary, root_cause, action, metric,
                before_value, after_value, source_incident_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(detector, stage) DO UPDATE SET
                 fault_mode=excluded.fault_mode, summary=excluded.summary,
                 root_cause=excluded.root_cause, action=excluded.action, metric=excluded.metric,
                 before_value=excluded.before_value, after_value=excluded.after_value,
                 source_incident_id=excluded.source_incident_id, updated_at=excluded.updated_at""",
            (detector, stage, fault_mode, summary, root_cause, action, metric,
             before_value, after_value, source_incident_id, now, now),
        )
        row = conn.execute(
            "SELECT id FROM regressions WHERE detector = ? AND stage IS ?", (detector, stage)
        ).fetchone()
        return row["id"]


def list_regressions(db_path: str) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM regressions ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_regression(db_path: str, regression_id: int) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM regressions WHERE id = ?", (regression_id,)).fetchone()
        return dict(row) if row else None


def record_regression_run(db_path: str, regression_id: int, *, passed: bool | None, detail: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE regressions SET last_run_at = ?, last_run_passed = ?, last_run_detail = ? WHERE id = ?",
            (time.time(), None if passed is None else int(passed), detail, regression_id),
        )


# ------------------------------------------------------------- blind eval runs --

def record_blind_eval_run(
    db_path: str, *, ts: float, trial_count: int, accuracy: float, counts: dict, trials: list[dict]
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO blind_eval_runs (ts, trial_count, accuracy, counts, trials) VALUES (?, ?, ?, ?, ?)",
            (ts, trial_count, accuracy, json.dumps(counts), json.dumps(trials)),
        )
        return cur.lastrowid


def list_blind_eval_runs(db_path: str, limit: int = 10) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM blind_eval_runs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["counts"] = json.loads(d["counts"])
            d["trials"] = json.loads(d["trials"])
            out.append(d)
        return out


# --------------------------------------------------------------- quality eval --

def record_quality_eval_run(
    db_path: str, *, ts: float, judge_model: str, prompt_count: int, pass_count: int,
    total_cost_usd: float, results: list[dict],
) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO quality_eval_runs
               (ts, judge_model, prompt_count, pass_count, total_cost_usd, results)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ts, judge_model, prompt_count, pass_count, total_cost_usd, json.dumps(results)),
        )
        return cur.lastrowid


def list_quality_eval_runs(db_path: str, limit: int = 10) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM quality_eval_runs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["results"] = json.loads(d["results"])
            out.append(d)
        return out
