"""Local SQLite store for the audit event trail (feature 042).

The queryable, full-payload half of obi's audit trail (Constitution Principle V): every LLM
communication, model tool call, device/vendor command, human turn, and report delivery lands
here as one row, grouped by session so whole conversations and watch cycles reconstruct in
causal order. The legacy JSONL trail (:mod:`obi.core.audit`) remains the crash-proof intent
log; this store adds filters, traces, and stats over the complete payloads.

Mirrors the :mod:`obi.core.logstore` idiom: this module is the only place permitted to import
``sqlite3`` for the audit store; WAL for concurrent writers (chat + cron ingests + watch), a
``user_version``-stamped schema with run-once migrations (feature 059, via
:func:`obi.core.storebase.apply_schema`), and an FTS5 external-content index for free-text
search. All query logic lives here so the CLI and web surfaces stay thin renderers
(Constitution Principle III).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obi.core.storebase import apply_schema

# Callers catch store failures through this alias instead of importing sqlite3 themselves.
StoreError = sqlite3.Error

# The event-kind vocabulary (data-model.md). `action` is the legacy AuditRecord tee.
KINDS = ("llm_call", "tool_call", "device_command", "human_message", "report", "action")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    ts_us          INTEGER NOT NULL,      -- epoch microseconds, UTC
    ts_iso         TEXT    NOT NULL,      -- tz-aware ISO-8601 for display
    kind           TEXT    NOT NULL,      -- one of KINDS (llm_call, tool_call, …)
    source         TEXT    NOT NULL,      -- chat|voice|slack|watch|cli:<command>
    session_id     TEXT,                  -- groups one conversation / watch cycle / CLI invocation
    target         TEXT,                  -- device/host/controller for device-facing events
    summary        TEXT    NOT NULL,      -- one line for list views
    payload        TEXT    NOT NULL,      -- kind-specific JSON object (bounded, post-redaction)
    duration_ms    REAL,
    outcome        TEXT    NOT NULL,      -- ok|error
    error          TEXT,
    truncated      INTEGER NOT NULL DEFAULT 0,
    original_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts_us);
CREATE INDEX IF NOT EXISTS idx_events_kind    ON events(kind, ts_us);
CREATE INDEX IF NOT EXISTS idx_events_source  ON events(source, ts_us);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts_us)
    WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_target  ON events(target, ts_us) WHERE target IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    summary, payload, content='events', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, summary, payload) VALUES (new.id, new.summary, new.payload);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_delete AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, summary, payload)
    VALUES ('delete', old.id, old.summary, old.payload);
END;
"""

# feature 059: ordered, run-once upgrades applied ON TOP OF the frozen v1 ``_SCHEMA`` above.
# ``_MIGRATIONS[i]`` takes the store from version ``i + 1`` to ``i + 2``. Append here for every
# schema change after v1 (e.g. ``"ALTER TABLE events ADD COLUMN ..."``); never edit ``_SCHEMA``.
_MIGRATIONS: tuple[str, ...] = ()


def connect(db_path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open the store read-write, creating it and its parent with the schema applied.

    ``check_same_thread=False`` is for the long-lived recorder, whose single connection is
    shared across the Slack listener's worker threads behind its own lock.
    """

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    apply_schema(conn, _SCHEMA, _MIGRATIONS)
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the store read-only (``mode=ro``) for querying — never creates or writes."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --- write path -------------------------------------------------------------------------------


def insert_event(
    conn: sqlite3.Connection,
    *,
    ts: datetime,
    kind: str,
    source: str,
    session_id: str | None,
    target: str | None,
    summary: str,
    payload: dict[str, Any],
    duration_ms: float | None,
    outcome: str,
    error: str | None,
    truncated: bool,
    original_bytes: int | None,
) -> int:
    """Insert one event and commit; returns the new event id."""

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    ts = ts.astimezone(UTC)
    cursor = conn.execute(
        """
        INSERT INTO events (ts_us, ts_iso, kind, source, session_id, target, summary, payload,
                            duration_ms, outcome, error, truncated, original_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(ts.timestamp() * 1_000_000),
            ts.isoformat(),
            kind,
            source,
            session_id,
            target,
            summary,
            json.dumps(payload, ensure_ascii=False, default=str),
            duration_ms,
            outcome,
            error,
            1 if truncated else 0,
            original_bytes,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def prune(conn: sqlite3.Connection, *, older_than: datetime) -> int:
    """Delete events older than ``older_than`` (the retention boundary); returns rows deleted."""

    cutoff_us = int(older_than.astimezone(UTC).timestamp() * 1_000_000)
    cursor = conn.execute("DELETE FROM events WHERE ts_us < ?", (cutoff_us,))
    conn.commit()
    return cursor.rowcount if cursor.rowcount is not None and cursor.rowcount > 0 else 0


# --- read path --------------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row, *, include_payload: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": row["id"],
        "ts": row["ts_iso"],
        "kind": row["kind"],
        "source": row["source"],
        "session_id": row["session_id"],
        "target": row["target"],
        "summary": row["summary"],
        "duration_ms": row["duration_ms"],
        "outcome": row["outcome"],
        "error": row["error"],
        "truncated": bool(row["truncated"]),
        "original_bytes": row["original_bytes"],
    }
    if include_payload:
        try:
            data["payload"] = json.loads(row["payload"])
        except ValueError:
            data["payload"] = {"raw": row["payload"]}
    return data


def _to_us(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.astimezone(UTC).timestamp() * 1_000_000)


def _filter_clauses(
    *,
    since: datetime | None,
    until: datetime | None,
    kinds: list[str] | None,
    sources: list[str] | None,
    session_id: str | None,
    target: str | None,
    outcome: str | None,
    text: str | None,
) -> tuple[str, list[Any]]:
    """Compose the WHERE clause (AND semantics) shared by list and stats queries."""

    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append("ts_us >= ?")
        params.append(_to_us(since))
    if until is not None:
        clauses.append("ts_us <= ?")
        params.append(_to_us(until))
    if kinds:
        clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    if sources:
        clauses.append(f"source IN ({','.join('?' * len(sources))})")
        params.extend(sources)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if target is not None:
        clauses.append("target = ?")
        params.append(target)
    if outcome is not None:
        clauses.append("outcome = ?")
        params.append(outcome)
    if text:
        # Quote the operator's text as one FTS5 string so raw punctuation cannot break the
        # query grammar (the logstore convention).
        quoted = '"' + text.replace('"', '""') + '"'
        clauses.append("id IN (SELECT rowid FROM events_fts WHERE events_fts MATCH ?)")
        params.append(quoted)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def query_events(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    kinds: list[str] | None = None,
    sources: list[str] | None = None,
    session_id: str | None = None,
    target: str | None = None,
    outcome: str | None = None,
    text: str | None = None,
    limit: int = 50,
    include_payload: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Newest-first filtered events plus the total match count (for "shown X of Y")."""

    where, params = _filter_clauses(
        since=since, until=until, kinds=kinds, sources=sources,
        session_id=session_id, target=target, outcome=outcome, text=text,
    )
    total_row = conn.execute(f"SELECT COUNT(*) FROM events{where}", params).fetchone()
    total = int(total_row[0])
    rows = conn.execute(
        f"SELECT * FROM events{where} ORDER BY ts_us DESC, id DESC LIMIT ?",
        [*params, max(0, limit)],
    ).fetchall()
    return [_row_to_dict(row, include_payload=include_payload) for row in rows], total


def get_event(conn: sqlite3.Connection, event_id: int) -> dict[str, Any] | None:
    """One event with its complete payload, or ``None`` when the id is unknown."""

    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_dict(row, include_payload=True) if row is not None else None


def session_trace(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """All events of one session in causal order (``ts_us`` then insertion id), with payloads."""

    rows = conn.execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY ts_us ASC, id ASC",
        (session_id,),
    ).fetchall()
    return [_row_to_dict(row, include_payload=True) for row in rows]


def stats(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    buckets: int = 48,
    top: int = 10,
) -> dict[str, Any]:
    """Aggregates for the stats/analytics views: counts, token usage, top tools/targets."""

    where, params = _filter_clauses(
        since=since, until=until, kinds=None, sources=None,
        session_id=None, target=None, outcome=None, text=None,
    )
    by_kind = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"SELECT kind, COUNT(*) FROM events{where} GROUP BY kind", params
        )
    }
    by_source = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"SELECT source, COUNT(*) FROM events{where} GROUP BY source", params
        )
    }
    llm_where = f"{where}{' AND' if where else ' WHERE'} kind = 'llm_call'"
    tokens_by_source: dict[str, dict[str, int]] = {}
    total_input = total_output = 0
    for row in conn.execute(
        "SELECT source, "
        "COALESCE(SUM(json_extract(payload, '$.input_tokens')), 0), "
        "COALESCE(SUM(json_extract(payload, '$.output_tokens')), 0) "
        f"FROM events{llm_where} GROUP BY source",
        params,
    ):
        tokens_by_source[str(row[0])] = {"input": int(row[1]), "output": int(row[2])}
        total_input += int(row[1])
        total_output += int(row[2])
    tool_where = f"{where}{' AND' if where else ' WHERE'} kind = 'tool_call'"
    top_tools = [
        {"tool": str(row[0]), "count": int(row[1])}
        for row in conn.execute(
            "SELECT json_extract(payload, '$.tool'), COUNT(*) "
            f"FROM events{tool_where} GROUP BY 1 ORDER BY 2 DESC LIMIT ?",
            [*params, top],
        )
        if row[0] is not None
    ]
    device_where = (
        f"{where}{' AND' if where else ' WHERE'} kind = 'device_command' AND target IS NOT NULL"
    )
    top_targets = [
        {"target": str(row[0]), "count": int(row[1])}
        for row in conn.execute(
            f"SELECT target, COUNT(*) FROM events{device_where} GROUP BY 1 ORDER BY 2 DESC LIMIT ?",
            [*params, top],
        )
    ]
    error_where = f"{where}{' AND' if where else ' WHERE'} outcome = 'error'"
    errors = int(conn.execute(f"SELECT COUNT(*) FROM events{error_where}", params).fetchone()[0])

    # Token usage over time: fixed-width buckets across the requested period (or the data's own
    # span when the period is open-ended). Empty buckets are included so charts render gaps.
    tokens_over_time: list[dict[str, Any]] = []
    span_row = conn.execute(f"SELECT MIN(ts_us), MAX(ts_us) FROM events{llm_where}", params)
    lo_hi = span_row.fetchone()
    if lo_hi is not None and lo_hi[0] is not None:
        lo = _to_us(since) if since is not None else int(lo_hi[0])
        hi = _to_us(until) if until is not None else int(lo_hi[1]) + 1
        width = max(1, (hi - lo) // max(1, buckets))
        rows = conn.execute(
            "SELECT (ts_us - ?) / ? AS bucket, "
            "COALESCE(SUM(json_extract(payload, '$.input_tokens')), 0), "
            "COALESCE(SUM(json_extract(payload, '$.output_tokens')), 0) "
            f"FROM events{llm_where} GROUP BY bucket ORDER BY bucket",
            [lo, width, *params],
        ).fetchall()
        filled = {int(row[0]): (int(row[1]), int(row[2])) for row in rows}
        count = max((b for b in filled), default=-1) + 1
        for index in range(count):
            start_us = lo + index * width
            moment = datetime.fromtimestamp(start_us / 1_000_000, tz=UTC)
            got = filled.get(index, (0, 0))
            tokens_over_time.append({"t": moment.isoformat(), "input": got[0], "output": got[1]})

    return {
        "by_kind": by_kind,
        "by_source": by_source,
        "tokens": {
            "total_input": total_input,
            "total_output": total_output,
            "by_source": tokens_by_source,
        },
        "tokens_over_time": tokens_over_time,
        "top_tools": top_tools,
        "top_targets": top_targets,
        "errors": errors,
    }


def store_status(db_path: Path) -> dict[str, Any]:
    """Store health for ``obi audit status`` / the web board — safe on an absent store."""

    if not db_path.exists():
        return {"present": False, "db_path": str(db_path)}
    conn = connect_readonly(db_path)
    try:
        count = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        span = conn.execute("SELECT MIN(ts_iso), MAX(ts_iso) FROM events").fetchone()
        day_ago_us = _to_us(datetime.now(UTC)) - 24 * 3600 * 1_000_000
        last_day = int(
            conn.execute("SELECT COUNT(*) FROM events WHERE ts_us >= ?", (day_ago_us,)).fetchone()[
                0
            ]
        )
    finally:
        conn.close()
    return {
        "present": True,
        "db_path": str(db_path),
        "size_bytes": db_path.stat().st_size,
        "event_count": count,
        "oldest": span[0],
        "newest": span[1],
        "events_last_24h": last_day,
    }
