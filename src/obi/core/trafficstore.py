"""Local SQLite store for BRC dashboard gate traffic (feature 112).

The 5-minute ``obi traffic run`` collector writes one **poll** row per cycle — the fetch
timestamp and each source's outcome (``new``/``unchanged``/``refused``/``failed``) — plus
whatever the cycle captured: Travel Time Log entries (append-only, deduped by ``entry_key``
because the feed re-serves the whole log), gate-status rows (only when the parsed status
actually changed), and the raw feed body (only on ``new``). The ``gate_traffic`` chat tool
and ``obi traffic status`` read it (read-only, via WAL, so a reader never blocks the
collector). The store IS the feature's degraded mode (Principle VI): with the uplink down —
or the feed still behind the pre-event 403 — the recorded outcomes and last entries keep
answering, with their ages; a refused or failed cycle still leaves a poll row, because the
outcome record is evidence, not noise.

Like ``weatherstore``/``icmpstore``, this is one of the few modules permitted to import
``sqlite3`` (Constitution Principle IV): schema, insert, prune, and the answer queries all
live here so the collector, CLI, and tool handler stay thin. Timestamps are epoch
microseconds UTC (``*_us``); rendering converts to playa-local. Raw payloads and per-entry
raw JSON are retained verbatim (spec FR-006/SC-004: every served number is traceable to a
stored payload).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from obi.core.storebase import apply_schema

# sqlite3 stays quarantined here (the logstore/metricstore rule): callers catch StoreError.
StoreError = sqlite3.Error
Connection = sqlite3.Connection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id              INTEGER PRIMARY KEY,
    fetched_us      INTEGER NOT NULL,   -- cycle wall-clock, epoch µs UTC (the check-age anchor)
    feed_outcome    TEXT NOT NULL,      -- new | unchanged | refused | failed
    feed_error      TEXT,               -- terse classification when refused/failed
    status_outcome  TEXT NOT NULL,      -- same vocabulary for /api/gate-status
    status_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_polls_fetched ON polls (fetched_us);

CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY,
    entry_key       TEXT NOT NULL UNIQUE,  -- feed id/uuid/key, else content hash
    published_us    INTEGER,               -- as parsed; NULL = no parseable stamp
    first_seen_us   INTEGER NOT NULL,      -- the capturing poll's fetched_us
    poll_id         INTEGER NOT NULL REFERENCES polls (id),
    title           TEXT,
    author          TEXT,
    pinned          INTEGER NOT NULL DEFAULT 0,
    text            TEXT NOT NULL,         -- served verbatim (raw JSON fallback, FR-006)
    raw             TEXT NOT NULL          -- the entry's own JSON, verbatim
);
CREATE INDEX IF NOT EXISTS idx_entries_published ON entries (published_us);

CREATE TABLE IF NOT EXISTS gate_statuses (
    id              INTEGER PRIMARY KEY,
    poll_id         INTEGER NOT NULL REFERENCES polls (id),
    captured_us     INTEGER NOT NULL,
    inbound_state   TEXT,               -- parsed lane state when recognizable
    outbound_state  TEXT,
    advisory        TEXT,               -- the payload's advisory/status text, verbatim
    raw             TEXT NOT NULL       -- the full response body, verbatim
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    poll_id         INTEGER NOT NULL REFERENCES polls (id),
    source          TEXT NOT NULL,
    body            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_state (
    source          TEXT PRIMARY KEY,   -- feed | status
    etag            TEXT,
    body_sha256     TEXT
);
"""

# feature-059 pattern: ordered, run-once upgrades ON TOP OF the frozen v1 ``_SCHEMA``. Append
# here for every schema change after v1; never edit ``_SCHEMA``.
_MIGRATIONS: tuple[str, ...] = ()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the store read-write for the collector, creating it + its parent with the schema."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    apply_schema(conn, _SCHEMA, _MIGRATIONS)
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the store read-only (``mode=ro``) for the tool/CLI — never creates or writes."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class ParsedEntry:
    """One Travel Time Log entry as parsed for storage (text/raw always served verbatim)."""

    entry_key: str
    published_us: int | None
    title: str | None
    author: str | None
    pinned: bool
    text: str
    raw: str


@dataclass(frozen=True)
class StoredEntry:
    """One stored entry plus its capture facts (``first_seen_us`` is the age fallback)."""

    entry_key: str
    published_us: int | None
    first_seen_us: int
    title: str | None
    author: str | None
    pinned: bool
    text: str
    raw: str


@dataclass(frozen=True)
class GateStatus:
    """One parsed gate status. All-None parsed fields = the published "not reported" state."""

    inbound_state: str | None
    outbound_state: str | None
    advisory: str | None
    raw: str


@dataclass(frozen=True)
class StoredGateStatus(GateStatus):
    """A stored status row with its capture time (the answer's as-of anchor)."""

    captured_us: int = 0


@dataclass(frozen=True)
class Poll:
    """One collector cycle's outcome, per source."""

    id: int
    fetched_us: int
    feed_outcome: str
    feed_error: str | None
    status_outcome: str
    status_error: str | None


@dataclass(frozen=True)
class FetchState:
    """Conditional-request memory for one source (research D3)."""

    etag: str | None
    body_sha256: str | None


def insert_cycle(
    conn: sqlite3.Connection,
    *,
    fetched_us: int,
    feed_outcome: str,
    feed_error: str | None,
    status_outcome: str,
    status_error: str | None,
    entries: Sequence[ParsedEntry],
    gate_status: GateStatus | None,
    feed_body: str | None,
) -> int:
    """Insert one cycle: the poll row (always), new entries (deduped), a gate-status row
    (only when the caller detected a change and passed one), and the raw feed body (only on
    ``new`` — the caller passes ``None`` otherwise). Returns the poll id. Does NOT commit —
    the caller commits so a whole cycle (insert + prune) is one atomic transaction (the
    metricstore rule)."""

    cur = conn.execute(
        "INSERT INTO polls (fetched_us, feed_outcome, feed_error, status_outcome, "
        "status_error) VALUES (?,?,?,?,?)",
        (fetched_us, feed_outcome, feed_error, status_outcome, status_error),
    )
    poll_id = int(cur.lastrowid or 0)
    conn.executemany(
        "INSERT OR IGNORE INTO entries (entry_key, published_us, first_seen_us, poll_id, "
        "title, author, pinned, text, raw) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                e.entry_key,
                e.published_us,
                fetched_us,
                poll_id,
                e.title,
                e.author,
                1 if e.pinned else 0,
                e.text,
                e.raw,
            )
            for e in entries
        ],
    )
    if gate_status is not None:
        conn.execute(
            "INSERT INTO gate_statuses (poll_id, captured_us, inbound_state, "
            "outbound_state, advisory, raw) VALUES (?,?,?,?,?,?)",
            (
                poll_id,
                fetched_us,
                gate_status.inbound_state,
                gate_status.outbound_state,
                gate_status.advisory,
                gate_status.raw,
            ),
        )
    if feed_body is not None:
        conn.execute(
            "INSERT INTO raw_payloads (poll_id, source, body) VALUES (?, 'feed', ?)",
            (poll_id, feed_body),
        )
    return poll_id


def set_fetch_state(
    conn: sqlite3.Connection, *, source: str, etag: str | None, body_sha256: str | None
) -> None:
    """Upsert one source's conditional-request memory. Caller commits."""

    conn.execute(
        "INSERT INTO fetch_state (source, etag, body_sha256) VALUES (?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET etag=excluded.etag, "
        "body_sha256=excluded.body_sha256",
        (source, etag, body_sha256),
    )


def get_fetch_state(conn: sqlite3.Connection, *, source: str) -> FetchState | None:
    row = conn.execute(
        "SELECT etag, body_sha256 FROM fetch_state WHERE source = ?", (source,)
    ).fetchone()
    if row is None:
        return None
    return FetchState(
        etag=str(row[0]) if row[0] is not None else None,
        body_sha256=str(row[1]) if row[1] is not None else None,
    )


def prune(conn: sqlite3.Connection, *, older_than_us: int) -> int:
    """Delete polls with ``fetched_us < older_than_us`` and their children (raw payloads,
    gate statuses), plus entries whose best timestamp is older than the horizon; return the
    number of polls removed. ``fetch_state`` survives. Caller commits. Children are deleted
    explicitly (SQLite only honors REFERENCES with a per-connection pragma; the store does
    not depend on it)."""

    conn.execute(
        "DELETE FROM raw_payloads WHERE poll_id IN "
        "(SELECT id FROM polls WHERE fetched_us < ?)",
        (older_than_us,),
    )
    # The NEWEST status row always survives (a long-unchanged status — the pre-event
    # all-null weeks — must keep serving as last-known-with-age, Principle VI).
    conn.execute(
        "DELETE FROM gate_statuses WHERE poll_id IN "
        "(SELECT id FROM polls WHERE fetched_us < ?) AND id != COALESCE("
        "(SELECT id FROM gate_statuses ORDER BY captured_us DESC, id DESC LIMIT 1), -1)",
        (older_than_us,),
    )
    conn.execute(
        "DELETE FROM entries WHERE COALESCE(published_us, first_seen_us) < ?",
        (older_than_us,),
    )
    cur = conn.execute("DELETE FROM polls WHERE fetched_us < ?", (older_than_us,))
    return cur.rowcount


def recent_entries(conn: sqlite3.Connection, *, limit: int) -> list[StoredEntry]:
    """The newest ``limit`` entries by ``COALESCE(published_us, first_seen_us)`` DESC — the
    tool's newest-report + trend material."""

    cur = conn.execute(
        "SELECT entry_key, published_us, first_seen_us, title, author, pinned, text, raw "
        "FROM entries ORDER BY COALESCE(published_us, first_seen_us) DESC, id DESC "
        "LIMIT ?",
        (limit,),
    )
    return [
        StoredEntry(
            entry_key=str(row[0]),
            published_us=int(row[1]) if row[1] is not None else None,
            first_seen_us=int(row[2]),
            title=str(row[3]) if row[3] is not None else None,
            author=str(row[4]) if row[4] is not None else None,
            pinned=bool(row[5]),
            text=str(row[6]),
            raw=str(row[7]),
        )
        for row in cur.fetchall()
    ]


def entry_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])


def newest_gate_status(conn: sqlite3.Connection) -> StoredGateStatus | None:
    """The most recent stored gate status (rows land only on change, so this is the
    current published state; its freshness anchor is the newest successful poll)."""

    row = conn.execute(
        "SELECT captured_us, inbound_state, outbound_state, advisory, raw "
        "FROM gate_statuses ORDER BY captured_us DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return StoredGateStatus(
        inbound_state=str(row[1]) if row[1] is not None else None,
        outbound_state=str(row[2]) if row[2] is not None else None,
        advisory=str(row[3]) if row[3] is not None else None,
        raw=str(row[4]),
        captured_us=int(row[0]),
    )


def newest_poll(conn: sqlite3.Connection) -> Poll | None:
    """The most recent cycle's outcome row — what keeps stale data and stale checks
    distinguishable in every answer."""

    row = conn.execute(
        "SELECT id, fetched_us, feed_outcome, feed_error, status_outcome, status_error "
        "FROM polls ORDER BY fetched_us DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return Poll(
        id=int(row[0]),
        fetched_us=int(row[1]),
        feed_outcome=str(row[2]),
        feed_error=str(row[3]) if row[3] is not None else None,
        status_outcome=str(row[4]),
        status_error=str(row[5]) if row[5] is not None else None,
    )


def coverage(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """(oldest, newest) poll ``fetched_us``, or ``None`` for a never-polled store."""

    row = conn.execute("SELECT MIN(fetched_us), MAX(fetched_us) FROM polls").fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0]), int(row[1])
