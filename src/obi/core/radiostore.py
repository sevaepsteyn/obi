"""Local SQLite store for the walkie-talkie radio surface (feature 088).

Three tables: ``transmissions`` (every on-air event — received traffic, obi's own
transmissions, and self-echo during our key-ups — one feed with one monotonic-rowid tail
cursor, the logstore idiom), ``calls`` (directed-call sessions), and ``outbound``
(everything obi said or *would have said*: shadow drafts, drops, and suppressions carry a
disposition and the deterministic gate's evaluation log, per contracts/tx-gate.md G11).

The invariant the web board and audit lean on: an outbound row with
``disposition='transmitted'`` and a ``transmissions`` row with ``kind='obi'`` exist
together or not at all — :func:`record_outbound` writes both in one transaction.

Timestamps are epoch µs UTC plus a pre-localized ``ts_iso`` display string rendered at
write time through the operator display timezone (the feature-069 rule: no new tz key —
callers pass ``config.logs.tzinfo()``). Retention is keep-everything by default
(``[radio] retention_days = 0``); :func:`prune` deletes rows older than a bound and
returns their clip paths so the caller can unlink the WAV files (this module never
touches the clip tree).

Like ``logstore``/``metricstore``/``icmpstore``, this is one of the few modules permitted
to import ``sqlite3`` (Constitution Principle IV); callers catch the re-exported
``StoreError``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from obi.core.storebase import apply_schema

# sqlite3 stays quarantined here (the logstore rule): callers catch StoreError.
StoreError = sqlite3.Error

TRANSMISSION_KINDS = ("rx", "obi", "self")
# The closed set of things obi can put on the air. Static, defined here, and extended ONLY
# by a code change — the model can neither add a kind nor reach the path that transmits one
# (tx-gate G10, restated by feature 092). 'operator' is `obi radio say`: a human's words,
# filed as such so the record never shows them as model output. 'garble_close' (093) is
# the canned on-air give-up ("unable to copy, out") — its own kind because the record
# must not file a protocol close as an unavailability (which closes calls as 'failure').
# 'hail'/'delivery' (124) are the outbound-call pair: the machine-rendered protocol hail
# ("<Name>, <Name>, obi") and the OPERATOR-fixed message it guards — text fixed at request
# time, never model output, and 'delivery' legal only after a heard go-ahead (contract O2).
OUTBOUND_KINDS = (
    "ack",
    "answer",
    "say_again",
    "unavailability",
    "standby",
    "operator",
    "garble_close",
    "hail",
    "delivery",
)
DISPOSITIONS = (
    "transmitted",
    "shadow_logged",
    "shadow_dmed",
    "dropped_busy",
    "suppressed_rate",
    "blocked_tx_off",
    "truncated_cap",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transmissions (
    id             INTEGER PRIMARY KEY,
    started_us     INTEGER NOT NULL,      -- epoch µs UTC
    ended_us       INTEGER NOT NULL,
    ts_iso         TEXT    NOT NULL,      -- pre-localized display timestamp (069 rule)
    duration_ms    INTEGER NOT NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('rx','obi','self')),
    peak_dbfs      REAL,                  -- NULL for kind='obi'
    rms_dbfs       REAL,
    transcript     TEXT    NOT NULL,      -- STT text (rx/self) or the spoken text (obi)
    stt_gate       TEXT    NOT NULL,      -- ok | no_speech | unintelligible
    clip_path      TEXT,                  -- relative to the clips dir; NULL if write failed
    trigger_fired  INTEGER NOT NULL DEFAULT 0,
    trigger_reason TEXT,                  -- the shadow-tuning record (rx only)
    caller         TEXT,                  -- handle from "this is <handle>", when stated
    call_id        INTEGER                -- calls.id when part of a call (either direction)
);
CREATE INDEX IF NOT EXISTS idx_radio_tx_started ON transmissions(started_us);
CREATE INDEX IF NOT EXISTS idx_radio_tx_call    ON transmissions(call_id);

CREATE VIRTUAL TABLE IF NOT EXISTS transmissions_fts USING fts5(
    transcript, content='transmissions', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS radio_tx_ai AFTER INSERT ON transmissions BEGIN
    INSERT INTO transmissions_fts (rowid, transcript) VALUES (new.id, new.transcript);
END;
CREATE TRIGGER IF NOT EXISTS radio_tx_ad AFTER DELETE ON transmissions BEGIN
    INSERT INTO transmissions_fts (transmissions_fts, rowid, transcript)
    VALUES ('delete', old.id, old.transcript);
END;

CREATE TABLE IF NOT EXISTS calls (
    id             INTEGER PRIMARY KEY,
    opened_us      INTEGER NOT NULL,
    closed_us      INTEGER,
    caller         TEXT,
    tier           TEXT    NOT NULL,      -- safe | operator
    state          TEXT    NOT NULL,
    closure        TEXT,                  -- cleared|timeout|garble|failure|dropped|shutdown
    say_again_used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS outbound (
    id                 INTEGER PRIMARY KEY,
    created_us         INTEGER NOT NULL,
    ts_iso             TEXT    NOT NULL,
    call_id            INTEGER NOT NULL,  -- no outbound exists outside a call (FR-025)
    kind               TEXT    NOT NULL
        CHECK (kind IN ('ack','answer','say_again','unavailability')),
    text               TEXT    NOT NULL,
    render_ms          INTEGER,           -- measured WAV duration (pre-flight cap input)
    disposition        TEXT    NOT NULL,
    gate_log           TEXT,              -- bounded JSON: clear checks, retries, rate state
    tx_transmission_id INTEGER            -- transmissions.id when disposition='transmitted'
);
CREATE INDEX IF NOT EXISTS idx_radio_outbound_call ON outbound(call_id);
"""

# feature-059 pattern: ordered, run-once upgrades ON TOP OF the frozen v1 ``_SCHEMA``.
# Append here for every schema change after v1; never edit ``_SCHEMA``.
_MIGRATIONS: tuple[str, ...] = (
    # v1 -> v2 (088 follow-up, 2026-08-20): passive speaker attribution for third-party
    # exchanges — the tracker's best guess and how sure it is ('high' = named on the air
    # via go-for/this-is/hail patterns; 'low' = alternation inference). NULL on rows
    # predating the migration, on obi's own rows, and on no_speech carrier rows.
    """
    ALTER TABLE transmissions ADD COLUMN speaker TEXT;
    ALTER TABLE transmissions ADD COLUMN speaker_confidence TEXT;
    """,
    # v2 -> v3 (2026-08-20): the 'standby' outbound kind (the radio "thinking…" — a
    # canned courtesy transmission when composing runs long). SQLite cannot widen a CHECK
    # constraint in place, so the outbound table is rebuilt with the expanded kind list
    # and its index recreated (DROP TABLE takes the renamed table's index with it).
    """
    ALTER TABLE outbound RENAME TO outbound_v2;
    CREATE TABLE outbound (
        id                 INTEGER PRIMARY KEY,
        created_us         INTEGER NOT NULL,
        ts_iso             TEXT    NOT NULL,
        call_id            INTEGER NOT NULL,
        kind               TEXT    NOT NULL
            CHECK (kind IN ('ack','answer','say_again','unavailability','standby')),
        text               TEXT    NOT NULL,
        render_ms          INTEGER,
        disposition        TEXT    NOT NULL,
        gate_log           TEXT,
        tx_transmission_id INTEGER
    );
    INSERT INTO outbound SELECT * FROM outbound_v2;
    DROP TABLE outbound_v2;
    CREATE INDEX IF NOT EXISTS idx_radio_outbound_call ON outbound(call_id);
    """,
    # v3 -> v4 (feature 092): operator test transmissions. Two changes, one rebuild.
    #
    # 1. The 'operator' outbound kind. `obi radio say` puts a HUMAN's words on the air; the
    #    record must not file them as an 'answer' the model composed. (This is the sixth
    #    kind; the tx-gate contract's "only four exist" was already stale at five.)
    # 2. `call_id` becomes NULLABLE. The v3 rebuild declared it NOT NULL, which would reject
    #    exactly the row this feature needs — an operator transmission has no call behind it.
    #
    # Same rebuild dance as v3: SQLite cannot widen a CHECK or drop a NOT NULL in place.
    """
    ALTER TABLE outbound RENAME TO outbound_v3;
    CREATE TABLE outbound (
        id                 INTEGER PRIMARY KEY,
        created_us         INTEGER NOT NULL,
        ts_iso             TEXT    NOT NULL,
        call_id            INTEGER,
        kind               TEXT    NOT NULL
            CHECK (kind IN ('ack','answer','say_again','unavailability','standby','operator')),
        text               TEXT    NOT NULL,
        render_ms          INTEGER,
        disposition        TEXT    NOT NULL,
        gate_log           TEXT,
        tx_transmission_id INTEGER
    );
    INSERT INTO outbound SELECT * FROM outbound_v3;
    DROP TABLE outbound_v3;
    CREATE INDEX IF NOT EXISTS idx_radio_outbound_call ON outbound(call_id);

    CREATE TABLE IF NOT EXISTS tx_requests (
        id           INTEGER PRIMARY KEY,
        created_us   INTEGER NOT NULL,
        ts_iso       TEXT    NOT NULL,
        expires_us   INTEGER NOT NULL,
        text         TEXT    NOT NULL,
        origin       TEXT    NOT NULL,
        state        TEXT    NOT NULL
            CHECK (state IN ('pending','claimed','done','expired')),
        claimed_us   INTEGER,
        settled_us   INTEGER,
        disposition  TEXT,
        detail       TEXT,
        outbound_id  INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_radio_txreq_state ON tx_requests(state, created_us);
    """,
    # v4 -> v5 (feature 093): the 'garble_close' outbound kind — the canned on-air close
    # both garble paths now transmit ("unable to copy, out") instead of ending in silence.
    # Its own kind for the 092 reason: reusing 'unavailability' would make the call
    # machine close the exchange as 'failure' and the record would misfile a protocol
    # close. Same rebuild dance as v3/v4: SQLite cannot widen a CHECK in place; call_id
    # stays nullable exactly as v4 declared it.
    """
    ALTER TABLE outbound RENAME TO outbound_v4;
    CREATE TABLE outbound (
        id                 INTEGER PRIMARY KEY,
        created_us         INTEGER NOT NULL,
        ts_iso             TEXT    NOT NULL,
        call_id            INTEGER,
        kind               TEXT    NOT NULL
            CHECK (kind IN ('ack','answer','say_again','unavailability','standby',
                            'operator','garble_close')),
        text               TEXT    NOT NULL,
        render_ms          INTEGER,
        disposition        TEXT    NOT NULL,
        gate_log           TEXT,
        tx_transmission_id INTEGER
    );
    INSERT INTO outbound SELECT * FROM outbound_v4;
    DROP TABLE outbound_v4;
    CREATE INDEX IF NOT EXISTS idx_radio_outbound_call ON outbound(call_id);
    """,
    # v5 -> v6 (feature 124): outbound calls — obi reaches out. Three changes, one step.
    #
    # 1. The 'hail'/'delivery' outbound kinds (same rebuild dance — SQLite cannot widen a
    #    CHECK in place). The hail is machine-rendered protocol text; the delivery is the
    #    OPERATOR's message, fixed at request time — neither is ever model output.
    # 2. calls.direction: 'inbound' (default — every pre-124 row and caller) | 'outbound',
    #    so the record and the board can tell who originated the exchange.
    # 3. The call_requests queue: the 092 tx_requests lifecycle plus a target and an
    #    attempt count. CLI/MCP enqueue here; the daemon claims (expiry checked at claim,
    #    the Q5 rule), hails, and settles with the outcome the requester polls for.
    """
    ALTER TABLE outbound RENAME TO outbound_v5;
    CREATE TABLE outbound (
        id                 INTEGER PRIMARY KEY,
        created_us         INTEGER NOT NULL,
        ts_iso             TEXT    NOT NULL,
        call_id            INTEGER,
        kind               TEXT    NOT NULL
            CHECK (kind IN ('ack','answer','say_again','unavailability','standby',
                            'operator','garble_close','hail','delivery')),
        text               TEXT    NOT NULL,
        render_ms          INTEGER,
        disposition        TEXT    NOT NULL,
        gate_log           TEXT,
        tx_transmission_id INTEGER
    );
    INSERT INTO outbound SELECT * FROM outbound_v5;
    DROP TABLE outbound_v5;
    CREATE INDEX IF NOT EXISTS idx_radio_outbound_call ON outbound(call_id);

    ALTER TABLE calls ADD COLUMN direction TEXT NOT NULL DEFAULT 'inbound';

    CREATE TABLE IF NOT EXISTS call_requests (
        id           INTEGER PRIMARY KEY,
        created_us   INTEGER NOT NULL,
        ts_iso       TEXT    NOT NULL,
        expires_us   INTEGER NOT NULL,
        callee       TEXT    NOT NULL,
        message      TEXT    NOT NULL,
        origin       TEXT    NOT NULL,
        state        TEXT    NOT NULL
            CHECK (state IN ('pending','claimed','done','expired')),
        attempts     INTEGER NOT NULL DEFAULT 0,
        claimed_us   INTEGER,
        settled_us   INTEGER,
        outcome      TEXT,
        detail       TEXT,
        call_id      INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_radio_callreq_state ON call_requests(state, created_us);
    """,
)


def connect(db_path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open the store read-write for the daemon, creating it + its parent with the schema.

    ``check_same_thread=False`` is the long-lived-daemon variant (the auditstore precedent):
    the runner's TX/compose worker threads record outbound rows; every write is serialized
    behind the runner's store lock.
    """

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    apply_schema(conn, _SCHEMA, _MIGRATIONS)
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the store read-only (``mode=ro``) for CLI/web queries — never creates or writes."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _iso(epoch_us: int, tz: tzinfo) -> str:
    """Render an epoch-µs instant as the operator-display ISO string (written once, at ingest)."""

    return datetime.fromtimestamp(epoch_us / 1_000_000, UTC).astimezone(tz).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


@dataclass(frozen=True)
class TransmissionRecord:
    """One transmission to record. ``caller``/``trigger_*`` are meaningful for ``rx`` only."""

    started_us: int
    ended_us: int
    kind: str
    transcript: str
    stt_gate: str
    peak_dbfs: float | None = None
    rms_dbfs: float | None = None
    clip_path: str | None = None
    trigger_fired: bool = False
    trigger_reason: str | None = None
    caller: str | None = None
    call_id: int | None = None
    speaker: str | None = None
    speaker_confidence: str | None = None


@dataclass(frozen=True)
class OutboundRecord:
    """Something obi said or would have said, with the gate's verdict.

    ``tx_started_us``/``tx_ended_us`` are the actual keyed window and are required exactly
    when ``disposition == 'transmitted'`` (they become the paired ``kind='obi'``
    transmissions row).
    """

    created_us: int
    call_id: int | None  # None for kind='operator' — a test transmission has no call
    kind: str
    text: str
    disposition: str
    render_ms: int | None = None
    gate_log: str | None = None
    tx_started_us: int | None = None
    tx_ended_us: int | None = None
    clip_path: str | None = None


@dataclass(frozen=True)
class Transmission:
    """A stored transmission as queried — obi rows carry their outbound metadata via join."""

    id: int
    started_us: int
    ended_us: int
    ts_iso: str
    duration_ms: int
    kind: str
    peak_dbfs: float | None
    rms_dbfs: float | None
    transcript: str
    stt_gate: str
    clip_path: str | None
    trigger_fired: bool
    trigger_reason: str | None
    caller: str | None
    call_id: int | None
    outbound_kind: str | None
    disposition: str | None
    speaker: str | None
    speaker_confidence: str | None


def record_transmission(conn: sqlite3.Connection, rec: TransmissionRecord, *, tz: tzinfo) -> int:
    """Insert one transmission (committing) and return its id — the tail cursor value."""

    if rec.kind not in TRANSMISSION_KINDS:
        raise ValueError(f"unknown transmission kind {rec.kind!r}")
    with conn:
        cursor = conn.execute(
            "INSERT INTO transmissions (started_us, ended_us, ts_iso, duration_ms, kind,"
            " peak_dbfs, rms_dbfs, transcript, stt_gate, clip_path, trigger_fired,"
            " trigger_reason, caller, call_id, speaker, speaker_confidence)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.started_us,
                rec.ended_us,
                _iso(rec.started_us, tz),
                max(0, (rec.ended_us - rec.started_us) // 1000),
                rec.kind,
                rec.peak_dbfs,
                rec.rms_dbfs,
                rec.transcript,
                rec.stt_gate,
                rec.clip_path,
                1 if rec.trigger_fired else 0,
                rec.trigger_reason,
                rec.caller,
                rec.call_id,
                rec.speaker,
                rec.speaker_confidence,
            ),
        )
    return int(cursor.lastrowid or 0)


def open_call(
    conn: sqlite3.Connection,
    *,
    opened_us: int,
    caller: str | None,
    tier: str,
    direction: str = "inbound",
) -> int:
    """Open a directed-call session in state ``acking``; returns the call id.

    ``direction`` (124): 'inbound' (someone hailed obi — every pre-124 caller) or
    'outbound' (obi hailed someone to deliver an operator-queued message).
    """

    with conn:
        cursor = conn.execute(
            "INSERT INTO calls (opened_us, caller, tier, state, direction) VALUES (?,?,?,?,?)",
            (opened_us, caller, tier, "acking", direction),
        )
    return int(cursor.lastrowid or 0)


def update_call(
    conn: sqlite3.Connection,
    call_id: int,
    *,
    state: str | None = None,
    caller: str | None = None,
    tier: str | None = None,
    say_again_used: bool | None = None,
) -> None:
    """Update the live fields of an open call (only the provided ones)."""

    sets: list[str] = []
    params: list[object] = []
    if state is not None:
        sets.append("state = ?")
        params.append(state)
    if caller is not None:
        sets.append("caller = ?")
        params.append(caller)
    if tier is not None:
        sets.append("tier = ?")
        params.append(tier)
    if say_again_used is not None:
        sets.append("say_again_used = ?")
        params.append(1 if say_again_used else 0)
    if not sets:
        return
    params.append(call_id)
    with conn:
        conn.execute(f"UPDATE calls SET {', '.join(sets)} WHERE id = ?", params)


def close_call(conn: sqlite3.Connection, call_id: int, *, closed_us: int, closure: str) -> None:
    """Terminate a call with its closure reason (state mirrors the closure)."""

    with conn:
        conn.execute(
            "UPDATE calls SET closed_us = ?, closure = ?, state = ? WHERE id = ?",
            (closed_us, closure, closure, call_id),
        )


def record_outbound(
    conn: sqlite3.Connection, rec: OutboundRecord, *, tz: tzinfo
) -> tuple[int, int | None]:
    """Record one outbound draft/transmission; returns ``(outbound_id, tx_transmission_id)``.

    The tx-gate invariant: ``disposition='transmitted'`` inserts the paired ``kind='obi'``
    transmissions row in the SAME transaction and links it; every other disposition leaves
    the feed untouched.
    """

    if rec.kind not in OUTBOUND_KINDS:
        raise ValueError(f"unknown outbound kind {rec.kind!r}")
    if rec.disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition {rec.disposition!r}")
    tx_id: int | None = None
    with conn:
        if rec.disposition == "transmitted":
            if rec.tx_started_us is None or rec.tx_ended_us is None:
                raise ValueError("transmitted outbound requires tx_started_us/tx_ended_us")
            cursor = conn.execute(
                "INSERT INTO transmissions (started_us, ended_us, ts_iso, duration_ms, kind,"
                " transcript, stt_gate, clip_path, call_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rec.tx_started_us,
                    rec.tx_ended_us,
                    _iso(rec.tx_started_us, tz),
                    max(0, (rec.tx_ended_us - rec.tx_started_us) // 1000),
                    "obi",
                    rec.text,
                    "ok",
                    rec.clip_path,
                    rec.call_id,
                ),
            )
            tx_id = int(cursor.lastrowid or 0)
        cursor = conn.execute(
            "INSERT INTO outbound (created_us, ts_iso, call_id, kind, text, render_ms,"
            " disposition, gate_log, tx_transmission_id)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                rec.created_us,
                _iso(rec.created_us, tz),
                rec.call_id,
                rec.kind,
                rec.text,
                rec.render_ms,
                rec.disposition,
                rec.gate_log,
                tx_id,
            ),
        )
    return int(cursor.lastrowid or 0), tx_id


# --- queries ------------------------------------------------------------------------------------

# speaker/speaker_confidence appended at the END (v2 columns) so the positional indices
# 0-16 in _row_to_transmission stay untouched.
_SELECT = (
    "SELECT t.id, t.started_us, t.ended_us, t.ts_iso, t.duration_ms, t.kind, t.peak_dbfs,"
    " t.rms_dbfs, t.transcript, t.stt_gate, t.clip_path, t.trigger_fired, t.trigger_reason,"
    " t.caller, t.call_id, o.kind AS outbound_kind, o.disposition AS disposition,"
    " t.speaker, t.speaker_confidence"
    " FROM transmissions t"
    " LEFT JOIN outbound o ON o.tx_transmission_id = t.id"
)


def _row_to_transmission(row: sqlite3.Row | tuple[object, ...]) -> Transmission:
    values = tuple(row)
    return Transmission(
        id=int(values[0]),
        started_us=int(values[1]),
        ended_us=int(values[2]),
        ts_iso=str(values[3]),
        duration_ms=int(values[4]),
        kind=str(values[5]),
        peak_dbfs=float(values[6]) if values[6] is not None else None,
        rms_dbfs=float(values[7]) if values[7] is not None else None,
        transcript=str(values[8]),
        stt_gate=str(values[9]),
        clip_path=str(values[10]) if values[10] is not None else None,
        trigger_fired=bool(values[11]),
        trigger_reason=str(values[12]) if values[12] is not None else None,
        caller=str(values[13]) if values[13] is not None else None,
        call_id=int(values[14]) if values[14] is not None else None,
        outbound_kind=str(values[15]) if values[15] is not None else None,
        disposition=str(values[16]) if values[16] is not None else None,
        speaker=str(values[17]) if values[17] is not None else None,
        speaker_confidence=str(values[18]) if values[18] is not None else None,
    )


def _fts_phrase(contains: str) -> str:
    """Quote operator text as an FTS5 phrase so it is matched literally, never parsed."""

    return '"' + contains.replace('"', "") + '"'


def latest_cursor(conn: sqlite3.Connection) -> int | None:
    """The newest transmission id — the tail watermark (ingest order, clock-skew-immune)."""

    row = conn.execute("SELECT MAX(id) FROM transmissions").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def query_since(
    conn: sqlite3.Connection, *, after_id: int, limit: int, include_self: bool = False
) -> list[Transmission]:
    """Tail read: transmissions with id > ``after_id``, ascending, at most ``limit``."""

    clauses = ["t.id > ?"]
    params: list[object] = [after_id]
    if not include_self:
        clauses.append("t.kind != 'self'")
    sql = f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY t.id ASC LIMIT ?"
    params.append(max(1, limit))
    return [_row_to_transmission(r) for r in conn.execute(sql, params).fetchall()]


def query_window(
    conn: sqlite3.Connection,
    *,
    limit: int,
    since_us: int | None = None,
    kind: str | None = None,
    contains: str | None = None,
    include_self: bool = False,
) -> list[Transmission]:
    """Initial read: the newest ≤``limit`` transmissions (optionally bounded/filtered),
    returned ascending. ``kind='self'`` explicitly includes only self rows; otherwise
    self rows are excluded unless ``include_self``."""

    clauses: list[str] = []
    params: list[object] = []
    join = ""
    if contains:
        join = " JOIN transmissions_fts f ON t.id = f.rowid"
        clauses.append("transmissions_fts MATCH ?")
        params.append(_fts_phrase(contains))
    if since_us is not None:
        clauses.append("t.started_us >= ?")
        params.append(since_us)
    if kind is not None:
        clauses.append("t.kind = ?")
        params.append(kind)
    elif not include_self:
        clauses.append("t.kind != 'self'")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"{_SELECT}{join}{where} ORDER BY t.id DESC LIMIT ?"
    params.append(max(1, limit))
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_transmission(r) for r in reversed(rows)]


@dataclass(frozen=True)
class TxRequest:
    """One queued operator transmission (`obi radio say`), and its outcome once settled."""

    id: int
    created_us: int
    ts_iso: str
    expires_us: int
    text: str
    origin: str
    state: str
    claimed_us: int | None
    settled_us: int | None
    disposition: str | None
    detail: str | None
    outbound_id: int | None


_TX_REQUEST_COLUMNS = (
    "id, created_us, ts_iso, expires_us, text, origin, state, claimed_us, settled_us,"
    " disposition, detail, outbound_id"
)


def _row_to_tx_request(row: sqlite3.Row | tuple[object, ...]) -> TxRequest:
    values = tuple(row)
    return TxRequest(
        id=int(values[0]),
        created_us=int(values[1]),
        ts_iso=str(values[2]),
        expires_us=int(values[3]),
        text=str(values[4]),
        origin=str(values[5]),
        state=str(values[6]),
        claimed_us=int(values[7]) if values[7] is not None else None,
        settled_us=int(values[8]) if values[8] is not None else None,
        disposition=str(values[9]) if values[9] is not None else None,
        detail=str(values[10]) if values[10] is not None else None,
        outbound_id=int(values[11]) if values[11] is not None else None,
    )


def enqueue_tx_request(
    conn: sqlite3.Connection,
    *,
    created_us: int,
    text: str,
    ttl_s: int,
    tz: tzinfo,
    origin: str = "cli",
) -> int:
    """Queue a message for the RUNNING station to transmit; return its id.

    The CLI cannot key the radio itself — the station holds an exclusive lock on the serial
    port, deliberately (a second holder is the stuck-PTT hazard). So `obi radio say` leaves
    the message here and the station picks it up. ``ttl_s`` bounds how long that is allowed
    to take: an unclaimed request expires rather than airing late (contract tx-request Q5).
    """

    with conn:
        cursor = conn.execute(
            "INSERT INTO tx_requests (created_us, ts_iso, expires_us, text, origin, state)"
            " VALUES (?,?,?,?,?,'pending')",
            (created_us, _iso(created_us, tz), created_us + ttl_s * 1_000_000, text, origin),
        )
    return int(cursor.lastrowid or 0)


def get_tx_request(conn: sqlite3.Connection, request_id: int) -> TxRequest | None:
    row = conn.execute(
        f"SELECT {_TX_REQUEST_COLUMNS} FROM tx_requests WHERE id = ?", (request_id,)
    ).fetchone()
    return _row_to_tx_request(row) if row is not None else None


def pending_tx_requests(conn: sqlite3.Connection) -> list[TxRequest]:
    """Unclaimed requests, oldest first."""

    rows = conn.execute(
        f"SELECT {_TX_REQUEST_COLUMNS} FROM tx_requests WHERE state = 'pending'"
        " ORDER BY created_us, id"
    ).fetchall()
    return [_row_to_tx_request(r) for r in rows]


def claim_tx_request(conn: sqlite3.Connection, request_id: int, *, now_us: int) -> bool:
    """Take exclusive ownership of a pending request. True ⇒ it is yours to transmit.

    Expiry is checked HERE, not only by a sweeper, so a request that sat through a daemon
    outage can never air at the next start-up (Q5). The claim itself is one conditional
    UPDATE checked by rowcount, so two claimers cannot both win (Q4) — a request is
    attempted at most once, and a gate refusal is reported rather than retried on the air.
    """

    with conn:
        expired = conn.execute(
            "UPDATE tx_requests SET state = 'expired', settled_us = ?"
            " WHERE id = ? AND state = 'pending' AND expires_us <= ?",
            (now_us, request_id, now_us),
        )
        if expired.rowcount:
            return False
        claimed = conn.execute(
            "UPDATE tx_requests SET state = 'claimed', claimed_us = ?"
            " WHERE id = ? AND state = 'pending'",
            (now_us, request_id),
        )
    return bool(claimed.rowcount)


def settle_tx_request(
    conn: sqlite3.Connection,
    request_id: int,
    *,
    settled_us: int,
    disposition: str,
    detail: str | None,
    outbound_id: int | None,
) -> None:
    """Record the terminal outcome the CLI is waiting to report back (Q8)."""

    with conn:
        conn.execute(
            "UPDATE tx_requests SET state = 'done', settled_us = ?, disposition = ?,"
            " detail = ?, outbound_id = ? WHERE id = ?",
            (settled_us, disposition, detail, outbound_id, request_id),
        )


# --- outbound call requests (feature 124) -------------------------------------------------------

#: Terminal outcomes a call request can settle with (contract O9). Static, like the kinds.
CALL_OUTCOMES = ("delivered", "unanswered", "declined", "expired", "failed")


@dataclass(frozen=True)
class CallRequest:
    """One queued outbound call (`obi radio call` / the MCP radio_call tool).

    ``message`` is the FIXED on-air delivery text — framed at submission, shown to the
    requester, transmitted verbatim (contract O3). ``attempts`` counts ack windows that
    elapsed unanswered; requeues (busy channel, inbound supersede) preserve it (O6).
    """

    id: int
    created_us: int
    ts_iso: str
    expires_us: int
    callee: str
    message: str
    origin: str
    state: str
    attempts: int
    claimed_us: int | None
    settled_us: int | None
    outcome: str | None
    detail: str | None
    call_id: int | None


_CALL_REQUEST_COLUMNS = (
    "id, created_us, ts_iso, expires_us, callee, message, origin, state, attempts,"
    " claimed_us, settled_us, outcome, detail, call_id"
)


def _row_to_call_request(row: sqlite3.Row | tuple[object, ...]) -> CallRequest:
    values = tuple(row)
    return CallRequest(
        id=int(values[0]),
        created_us=int(values[1]),
        ts_iso=str(values[2]),
        expires_us=int(values[3]),
        callee=str(values[4]),
        message=str(values[5]),
        origin=str(values[6]),
        state=str(values[7]),
        attempts=int(values[8]),
        claimed_us=int(values[9]) if values[9] is not None else None,
        settled_us=int(values[10]) if values[10] is not None else None,
        outcome=str(values[11]) if values[11] is not None else None,
        detail=str(values[12]) if values[12] is not None else None,
        call_id=int(values[13]) if values[13] is not None else None,
    )


def enqueue_call_request(
    conn: sqlite3.Connection,
    *,
    created_us: int,
    callee: str,
    message: str,
    ttl_s: int,
    tz: tzinfo,
    origin: str = "cli",
) -> int:
    """Queue an outbound call for the RUNNING station; return its id.

    Same reasoning as :func:`enqueue_tx_request`: the originators cannot key the radio
    (the daemon owns the serial lock), so they leave the request here. ``ttl_s`` bounds
    the whole attempt — enqueue through delivery — so a request never airs stale (O5).
    """

    with conn:
        cursor = conn.execute(
            "INSERT INTO call_requests"
            " (created_us, ts_iso, expires_us, callee, message, origin, state)"
            " VALUES (?,?,?,?,?,?,'pending')",
            (
                created_us,
                _iso(created_us, tz),
                created_us + ttl_s * 1_000_000,
                callee,
                message,
                origin,
            ),
        )
    return int(cursor.lastrowid or 0)


def get_call_request(conn: sqlite3.Connection, request_id: int) -> CallRequest | None:
    row = conn.execute(
        f"SELECT {_CALL_REQUEST_COLUMNS} FROM call_requests WHERE id = ?", (request_id,)
    ).fetchone()
    return _row_to_call_request(row) if row is not None else None


def pending_call_requests(conn: sqlite3.Connection) -> list[CallRequest]:
    """Unclaimed outbound calls, oldest first (submission order — contract/spec FR-014)."""

    rows = conn.execute(
        f"SELECT {_CALL_REQUEST_COLUMNS} FROM call_requests WHERE state = 'pending'"
        " ORDER BY created_us, id"
    ).fetchall()
    return [_row_to_call_request(r) for r in rows]


def claim_call_request(conn: sqlite3.Connection, request_id: int, *, now_us: int) -> bool:
    """Take exclusive ownership of a pending outbound call. True ⇒ it is yours to hail.

    The tx_requests claim shape (Q4/Q5): expiry is checked HERE so a request that sat
    through a daemon outage settles 'expired' instead of hailing hours late, and the
    claim is one conditional UPDATE checked by rowcount so two claimers cannot both win.
    """

    with conn:
        expired = conn.execute(
            "UPDATE call_requests SET state = 'expired', outcome = 'expired',"
            " settled_us = ?, detail = 'expired before the station could hail'"
            " WHERE id = ? AND state = 'pending' AND expires_us <= ?",
            (now_us, request_id, now_us),
        )
        if expired.rowcount:
            return False
        claimed = conn.execute(
            "UPDATE call_requests SET state = 'claimed', claimed_us = ?"
            " WHERE id = ? AND state = 'pending'",
            (now_us, request_id),
        )
    return bool(claimed.rowcount)


def requeue_call_request(
    conn: sqlite3.Connection,
    request_id: int,
    *,
    attempts: int,
    call_id: int | None,
    detail: str | None = None,
) -> None:
    """Put a claimed request back in line WITHOUT consuming a retry (contract O6/O7).

    Used when the attempt yielded for reasons that are not the callee's silence: a busy
    channel dropped the hail, or an inbound caller outranked the attempt. ``attempts``
    persists the machine's count (windows that actually elapsed); ``call_id`` keeps the
    last attempt's exchange row for the audit trail.
    """

    with conn:
        conn.execute(
            "UPDATE call_requests SET state = 'pending', claimed_us = NULL,"
            " attempts = ?, call_id = ?, detail = ? WHERE id = ? AND state = 'claimed'",
            (attempts, call_id, detail, request_id),
        )


def settle_call_request(
    conn: sqlite3.Connection,
    request_id: int,
    *,
    settled_us: int,
    outcome: str,
    attempts: int,
    detail: str | None,
    call_id: int | None,
) -> None:
    """Record the terminal outcome the requester is polling for (contract O9)."""

    if outcome not in CALL_OUTCOMES:
        raise ValueError(f"unknown call outcome {outcome!r}")
    with conn:
        conn.execute(
            "UPDATE call_requests SET state = 'done', settled_us = ?, outcome = ?,"
            " attempts = ?, detail = ?, call_id = ? WHERE id = ?",
            (settled_us, outcome, attempts, detail, call_id, request_id),
        )


def prune(conn: sqlite3.Connection, *, older_than_us: int) -> list[str]:
    """Delete transmissions started before the bound; return their clip paths for unlinking.

    The FTS delete-trigger keeps the index in sync. Calls/outbound rows are kept — they are
    the audit-adjacent record and tiny compared to transcripts+clips. Callers only invoke
    this when ``[radio] retention_days > 0`` (the default 0 keeps everything).
    """

    rows = conn.execute(
        "SELECT clip_path FROM transmissions WHERE started_us < ? AND clip_path IS NOT NULL",
        (older_than_us,),
    ).fetchall()
    with conn:
        conn.execute("DELETE FROM transmissions WHERE started_us < ?", (older_than_us,))
        # 092: operator requests are a work queue, not a record — the transmission they
        # produced is preserved in outbound/transmissions. Prune them on the same bound.
        conn.execute("DELETE FROM tx_requests WHERE created_us < ?", (older_than_us,))
        # 124: outbound call requests are the same class of work queue.
        conn.execute("DELETE FROM call_requests WHERE created_us < ?", (older_than_us,))
    return [str(r[0]) for r in rows]
