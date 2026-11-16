"""Local SQLite store for ingested device syslog (feature 013).

obi never touches the syslog-ng collectors directly: a scheduled rsync mirrors their per-host
daily files into a local tree (see ``specs/013-syslog-query/quickstart.md``), and this module
parses that mirror into a queryable SQLite database with an FTS5 full-text index. It is the only
place permitted to import ``sqlite3``, mirroring how ``httpx`` is quarantined to the vendor
adapters (Constitution Principle IV); the schema, the RouterOS line parser, the incremental+dedup
ingest, and the read query all live here so the driver and CLI stay thin.

Named ``logstore`` (not ``logging``) to avoid colliding with :mod:`obi.core.logging`.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path

from obi.core.models import LogEvent
from obi.core.storebase import apply_schema

# sqlite3 is quarantined to this module (see the module docstring); callers that need to
# catch store failures do so through this alias instead of importing sqlite3 themselves.
StoreError = sqlite3.Error

# --- schema -----------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY,
    ts_us       INTEGER NOT NULL,         -- epoch microseconds, UTC-normalized
    ts_iso      TEXT    NOT NULL,         -- tz-aware ISO-8601 for display
    host        TEXT    NOT NULL,         -- authoritative host (filename stem)
    subsystem   TEXT,                     -- first RouterOS topic token (e.g. "dhcp")
    topics      TEXT    NOT NULL,         -- full csv as emitted (e.g. "dhcp,info")
    severity    TEXT,                     -- derived keyword or NULL
    message     TEXT    NOT NULL,
    raw_line    TEXT    NOT NULL,
    collectors  TEXT    NOT NULL,         -- csv of collectors that saw it (e.g. "core,chestnut")
    dedup_key   TEXT    NOT NULL UNIQUE   -- sha256(ts_us | host | message)
);
CREATE INDEX IF NOT EXISTS idx_logs_host_ts  ON logs(host, ts_us);
CREATE INDEX IF NOT EXISTS idx_logs_ts       ON logs(ts_us);
CREATE INDEX IF NOT EXISTS idx_logs_severity ON logs(severity) WHERE severity IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(
    message, host, topics, content='logs', content_rowid='id', tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS ingest_state (
    source_file TEXT PRIMARY KEY,
    size_bytes  INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,         -- bytes already parsed (resume point)
    updated_at  TEXT    NOT NULL
);

-- feature 016: per-site UniFi controller-event ingest watermark (the API poll has no file offset).
-- last_ts_ms is the max event timestamp ingested; last_event_id breaks ties at that timestamp.
CREATE TABLE IF NOT EXISTS unifi_watermark (
    site          TEXT PRIMARY KEY,
    last_ts_ms    INTEGER NOT NULL,
    last_event_id TEXT,
    updated_at    TEXT    NOT NULL
);

-- feature 019: per-source UISP controller ingest watermark. "source" is the controller read
-- (``logs`` or ``outages``); UISP /logs and /outages are fleet-global, so the natural key is the
-- source, not a site. last_ts_ms is the max record timestamp ingested; last_id breaks ties.
CREATE TABLE IF NOT EXISTS uisp_watermark (
    source     TEXT PRIMARY KEY,
    last_ts_ms INTEGER NOT NULL,
    last_id    TEXT,
    updated_at TEXT    NOT NULL
);
"""

# feature 059: ordered, run-once upgrades applied ON TOP OF the frozen v1 ``_SCHEMA`` above.
# ``_MIGRATIONS[i]`` takes the store from version ``i + 1`` to ``i + 2``. Append here for every
# schema change after v1 (e.g. ``"ALTER TABLE logs ADD COLUMN ..."``); never edit ``_SCHEMA``.
_MIGRATIONS: tuple[str, ...] = ()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the store read-write for ingest, creating it and its parent with the schema applied."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    apply_schema(conn, _SCHEMA, _MIGRATIONS)
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the store read-only (``mode=ro``) for querying — never creates or writes."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --- parser -----------------------------------------------------------------------------------

_BSD_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Mon day HH:MM:SS[.frac] host topics-csv message...  (day may be space-padded; frac optional)
_LINE_RE = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+"
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})(?:\.(?P<frac>\d+))?\s+"
    r"(?P<host>\S+)\s+(?P<topics>\S+)\s+(?P<msg>.*)$"
)

# RouterOS severity-ish topic keywords, with the short forms it also emits normalized.
_SEVERITIES = {
    "debug": "debug", "info": "info", "warning": "warning", "warn": "warning",
    "error": "error", "err": "error", "critical": "critical", "crit": "critical",
}

# The normalized severities in ascending order — the vocabulary for "floor" queries (038).
SEVERITY_ORDER = ("debug", "info", "warning", "error", "critical")


def severities_at_or_above(floor: str) -> tuple[str, ...]:
    """The normalized severities >= ``floor`` (e.g. ``"warning"`` -> warning/error/critical).

    Raises :class:`ValueError` on an unknown floor so callers can 400 cleanly.
    """

    if floor not in SEVERITY_ORDER:
        raise ValueError(f"unknown severity {floor!r} (one of {', '.join(SEVERITY_ORDER)})")
    return SEVERITY_ORDER[SEVERITY_ORDER.index(floor):]


def _derive_severity(topic_tokens: list[str]) -> str | None:
    """First severity-like keyword among the RouterOS topics (after the subsystem), normalized."""

    for token in topic_tokens[1:]:
        sev = _SEVERITIES.get(token.lower())
        if sev is not None:
            return sev
    return None


def parse_line(
    path_date: date, collector: str, host: str, line: str, *, tz: tzinfo
) -> LogEvent | None:
    """Parse one RouterOS BSD-syslog line into a :class:`LogEvent`, or ``None`` if malformed.

    The line carries no year, so the full timestamp is the ``path_date`` (from the mirror's
    ``YYYY/MM/DD`` segments — authoritative) merged with the line's time-of-day, made tz-aware in
    ``tz``. ``host`` (the filename stem) is authoritative; a differing in-line host is kept only as
    a note. Never raises — a line obi cannot parse returns ``None`` so the ingest counts and skips
    it rather than aborting.
    """

    match = _LINE_RE.match(line.rstrip("\n"))
    if match is None:
        return None
    if match["mon"] not in _BSD_MONTHS:
        return None
    hour, minute, second = int(match["h"]), int(match["m"]), int(match["s"])
    if hour > 23 or minute > 59 or second > 59:
        return None
    frac = (match["frac"] or "")[:6].ljust(6, "0")
    micros = int(frac) if frac else 0
    try:
        ts = datetime(
            path_date.year, path_date.month, path_date.day,
            hour, minute, second, micros, tzinfo=tz,
        )
    except ValueError:
        return None

    topics = match["topics"]
    topic_tokens = topics.split(",")
    notes: list[str] = []
    line_host = match["host"]
    if line_host != host:
        notes.append(f"line host {line_host!r} != file host {host!r}")
    return LogEvent(
        ts=ts,
        host=host,
        topics=topics,
        message=match["msg"],
        subsystem=topic_tokens[0] or None,
        severity=_derive_severity(topic_tokens),
        raw_line=line.rstrip("\n"),
        collectors=[collector],
        notes=notes,
    )


# --- ingest -----------------------------------------------------------------------------------


@dataclass
class IngestStats:
    """Summary of one :func:`ingest_tree` run (printed by ``obi logs ingest``; never secret)."""

    files_scanned: int = 0
    files_ingested: int = 0  # files that had new bytes parsed
    lines_parsed: int = 0
    inserted: int = 0
    deduped: int = 0  # lines whose logical event already existed (collector possibly merged)
    skipped_malformed: int = 0


# A per-host daily file: <mirror>/<collector>/YYYY/MM/DD/<host>.log (messages.log is excluded).
_TREE_GLOB = "*/[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*.log"


def _to_us(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000)


def _iter_daily_files(mirror_root: Path) -> Iterator[Path]:
    for path in sorted(mirror_root.glob(_TREE_GLOB)):
        if path.is_file() and path.stem != "messages":
            yield path


def _path_date(path: Path, mirror_root: Path) -> tuple[date, str] | None:
    """Return ``(path_date, collector)`` from ``<collector>/YYYY/MM/DD/host.log``, or ``None``."""

    rel = path.relative_to(mirror_root).parts
    if len(rel) < 5:
        return None
    collector, year, month, day = rel[0], rel[1], rel[2], rel[3]
    try:
        return date(int(year), int(month), int(day)), collector
    except ValueError:
        return None


def _insert_event(
    conn: sqlite3.Connection, event: LogEvent, *, dedup_key: str | None = None
) -> bool:
    """Insert one event (deduped). Returns ``True`` if a new row was created, ``False`` if it
    already existed (in which case the seeing collector is merged into ``collectors``).

    ``dedup_key`` defaults to ``sha256(ts_us | host | message)`` (the RouterOS identity). A caller
    with a better stable identity — e.g. UniFi's per-event ``id`` (feature 016) — may pass an
    explicit key so distinct same-second events are not collapsed.
    """

    ts_us = _to_us(event.ts)
    collector = event.collectors[0] if event.collectors else ""
    if dedup_key is None:
        dedup_key = hashlib.sha256(
            f"{ts_us}\x1f{event.host}\x1f{event.message}".encode()
        ).hexdigest()
    cur = conn.execute(
        "INSERT OR IGNORE INTO logs "
        "(ts_us, ts_iso, host, subsystem, topics, severity, message, raw_line, collectors, "
        "dedup_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            ts_us, event.ts.isoformat(), event.host, event.subsystem, event.topics,
            event.severity, event.message, event.raw_line, collector, dedup_key,
        ),
    )
    if cur.rowcount == 1:
        conn.execute(
            "INSERT INTO logs_fts (rowid, message, host, topics) VALUES (?,?,?,?)",
            (cur.lastrowid, event.message, event.host, event.topics),
        )
        return True
    # Already present: record this collector too, if new (collector names have no LIKE wildcards).
    conn.execute(
        "UPDATE logs SET collectors = collectors || ',' || ? "
        "WHERE dedup_key = ? AND ',' || collectors || ',' NOT LIKE '%,' || ? || ',%'",
        (collector, dedup_key, collector),
    )
    return False


def ingest_file(
    conn: sqlite3.Connection,
    path: Path,
    path_date: date,
    collector: str,
    *,
    tz: tzinfo,
    stats: IngestStats,
) -> None:
    """Incrementally ingest one mirrored daily file from its last byte offset (idempotent).

    Unchanged files (same size+mtime as last run) are skipped — past days' files are immutable, so
    this is the fast path. Today's growing file is read from its stored offset; a truncated/rotated
    file is re-read from 0 (dedup keeps that safe). The cursor advances only past a complete
    trailing newline, in the same transaction as the row inserts, so a crash never loses or
    half-counts a line.
    """

    st = path.stat()
    row = conn.execute(
        "SELECT size_bytes, mtime_ns, byte_offset FROM ingest_state WHERE source_file = ?",
        (str(path),),
    ).fetchone()
    offset = 0
    if row is not None:
        prev_size, prev_mtime, prev_offset = int(row[0]), int(row[1]), int(row[2])
        if prev_size == st.st_size and prev_mtime == st.st_mtime_ns:
            return  # unchanged
        offset = prev_offset if st.st_size >= prev_size else 0

    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
    # Only parse up to the last complete line; a partial final line is re-read next run.
    last_nl = chunk.rfind(b"\n")
    consumed = last_nl + 1 if last_nl >= 0 else 0
    text = chunk[:consumed].decode("utf-8", errors="replace")

    had_new = False
    for raw in text.splitlines():
        if not raw.strip():
            continue
        had_new = True
        stats.lines_parsed += 1
        event = parse_line(path_date, collector, path.stem, raw, tz=tz)
        if event is None:
            stats.skipped_malformed += 1
            continue
        if _insert_event(conn, event):
            stats.inserted += 1
        else:
            stats.deduped += 1

    conn.execute(
        "INSERT INTO ingest_state (source_file, size_bytes, mtime_ns, byte_offset, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(source_file) DO UPDATE SET "
        "size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns, "
        "byte_offset=excluded.byte_offset, updated_at=excluded.updated_at",
        (str(path), st.st_size, st.st_mtime_ns, offset + consumed,
         datetime.now(UTC).isoformat()),
    )
    conn.commit()
    if had_new:
        stats.files_ingested += 1


def ingest_tree(conn: sqlite3.Connection, mirror_root: Path, *, tz: tzinfo) -> IngestStats:
    """Ingest every mirrored per-host daily file under ``mirror_root`` (incremental + deduped)."""

    stats = IngestStats()
    for path in _iter_daily_files(mirror_root):
        resolved = _path_date(path, mirror_root)
        if resolved is None:
            continue
        path_date, collector = resolved
        stats.files_scanned += 1
        ingest_file(conn, path, path_date, collector, tz=tz, stats=stats)
    return stats


# --- pre-built event ingest (feature 016: UniFi controller events) ----------------------------


def ingest_events(
    conn: sqlite3.Connection,
    events: Iterable[tuple[LogEvent, str | None]],
    *,
    stats: IngestStats | None = None,
) -> IngestStats:
    """Insert already-parsed :class:`LogEvent`\\ s (e.g. UniFi controller events) into the store.

    Each item is ``(event, dedup_key)``; a ``None`` key falls back to the default
    ``sha256(ts | host | message)``. Idempotent — a row whose ``dedup_key`` already exists is
    skipped (and counted as deduped). Does **not** commit: the caller commits so a per-site cycle
    (rows + watermark) is one atomic transaction.
    """

    stats = stats or IngestStats()
    for event, dedup_key in events:
        stats.lines_parsed += 1
        if _insert_event(conn, event, dedup_key=dedup_key):
            stats.inserted += 1
        else:
            stats.deduped += 1
    return stats


def get_unifi_watermark(conn: sqlite3.Connection, site: str) -> tuple[int, str | None] | None:
    """Return ``(last_ts_ms, last_event_id)`` for a site, or ``None`` if it has never ingested."""

    row = conn.execute(
        "SELECT last_ts_ms, last_event_id FROM unifi_watermark WHERE site = ?", (site,)
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), (str(row[1]) if row[1] is not None else None)


def set_unifi_watermark(
    conn: sqlite3.Connection, site: str, last_ts_ms: int, last_event_id: str | None
) -> None:
    """Persist a site's ingest watermark (durable across restarts). Caller commits."""

    conn.execute(
        "INSERT INTO unifi_watermark (site, last_ts_ms, last_event_id, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(site) DO UPDATE SET "
        "last_ts_ms=excluded.last_ts_ms, last_event_id=excluded.last_event_id, "
        "updated_at=excluded.updated_at",
        (site, last_ts_ms, last_event_id, datetime.now(UTC).isoformat()),
    )


def list_unifi_watermarks(conn: sqlite3.Connection) -> list[tuple[str, int, str | None, str]]:
    """Per-site UniFi watermarks as (site, last_ts_ms, last_event_id, updated_at). Read-only.

    Returns ``[]`` when the ``unifi_watermark`` table does not exist yet — a read-only store opened
    before any UniFi ingest (e.g. a pre-016 store) never created it, and the query path must
    degrade, never raise.
    """

    try:
        rows = conn.execute(
            "SELECT site, last_ts_ms, last_event_id, updated_at FROM unifi_watermark ORDER BY site"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # table absent (store predates feature 016 / no UniFi ingest has run)
    return [
        (str(r[0]), int(r[1]), str(r[2]) if r[2] is not None else None, str(r[3]))
        for r in rows
    ]


# --- UISP per-source ingest watermark (feature 019) -------------------------------------------


def get_uisp_watermark(conn: sqlite3.Connection, source: str) -> tuple[int, str | None] | None:
    """Return ``(last_ts_ms, last_id)`` for a source, or ``None`` if it has never ingested."""

    row = conn.execute(
        "SELECT last_ts_ms, last_id FROM uisp_watermark WHERE source = ?", (source,)
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), (str(row[1]) if row[1] is not None else None)


def set_uisp_watermark(
    conn: sqlite3.Connection, source: str, last_ts_ms: int, last_id: str | None
) -> None:
    """Persist a source's ingest watermark (durable across restarts). Caller commits."""

    conn.execute(
        "INSERT INTO uisp_watermark (source, last_ts_ms, last_id, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
        "last_ts_ms=excluded.last_ts_ms, last_id=excluded.last_id, updated_at=excluded.updated_at",
        (source, last_ts_ms, last_id, datetime.now(UTC).isoformat()),
    )


def list_uisp_watermarks(conn: sqlite3.Connection) -> list[tuple[str, int, str | None, str]]:
    """Per-source UISP watermarks as (source, last_ts_ms, last_id, updated_at). Read-only.

    Returns ``[]`` when the ``uisp_watermark`` table does not exist yet — a read-only store opened
    before any UISP ingest (a pre-019 store) never created it, and the query path must degrade.
    """

    try:
        rows = conn.execute(
            "SELECT source, last_ts_ms, last_id, updated_at FROM uisp_watermark ORDER BY source"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # table absent (store predates feature 019 / no UISP ingest has run)
    return [
        (str(r[0]), int(r[1]), str(r[2]) if r[2] is not None else None, str(r[3]))
        for r in rows
    ]


# --- query ------------------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"(?P<n>\d+)\s*(?P<unit>[smhdw])")
_RELATIVE_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_when(value: object, tz: tzinfo) -> datetime | None:
    """Parse a since/until value used by both the chat tool and the CLI.

    Accepts a relative lookback from now (``1h`` / ``30m`` / ``2d`` / ``1w``) or an ISO date /
    datetime (``2026-06-03`` or ``2026-06-03T10:00``). A naive ISO value is interpreted in ``tz``.
    Unparseable input returns ``None`` (a bad filter is ignored, never an error).
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    rel = _RELATIVE_RE.fullmatch(text.lower())
    if rel is not None:
        seconds = int(rel["n"]) * _RELATIVE_SECONDS[rel["unit"]]
        return datetime.now(tz) - timedelta(seconds=seconds)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=tz)


def _fts_phrase(contains: str) -> str:
    """Quote arbitrary operator text as an FTS5 phrase so it is matched literally, never parsed
    as LogsQL/FTS syntax (which would error or surprise)."""

    return '"' + contains.replace('"', '""') + '"'


# --- source families (feature 050) ------------------------------------------------------------
#
# The `source` filter selects rows by their `collectors` tag. Every value is a case-insensitive
# SUBSTRING on `collectors` EXCEPT the one reserved word below.
#
# SYSLOG_SOURCE_FAMILY ("syslog") is resolved by EXCLUSION: a row is device syslog (feature 013)
# iff none of its csv collector tokens is a declared controller tag. CONTROLLER_COLLECTOR_TAGS is
# the single declared authority for those non-syslog tags. Each member is assigned at ingest by its
# adapter — `"unifi"` in `drivers/unifi_common.py` (016), `"uisp"` in `drivers/uisp.py` (019); the
# device-syslog ingest above tags rows with the collector HOSTNAME (a variable), so a new syslog
# collector host is included by "syslog" with no code change. tests/unit/test_collector_families.py
# fails if a new ingest path introduces a literal collector tag not declared here (FR-006).
SYSLOG_SOURCE_FAMILY = "syslog"
CONTROLLER_COLLECTOR_TAGS: frozenset[str] = frozenset({"unifi", "uisp"})


def _filter_clauses(
    *,
    host: str | None,
    since: datetime | None,
    until: datetime | None,
    severity: str | None,
    min_severity: str | None,
    contains: str | None,
    source: str | None,
) -> tuple[str, list[str], list[object]]:
    """The shared WHERE vocabulary for log queries: ``(fts_join, clauses, params)``.

    One builder so the newest-first read (:func:`query_events`) and the id-ordered tail
    (:func:`query_since`, 038) can never drift in filter semantics. ``severity`` is an exact
    match (the 013 CLI contract); ``min_severity`` is a floor over :data:`SEVERITY_ORDER`
    (NULL-severity rows are excluded under a floor — they cannot be ranked).
    """

    where: list[str] = []
    params: list[object] = []
    join = ""
    if contains:
        join = " JOIN logs_fts ON logs.id = logs_fts.rowid"
        where.append("logs_fts MATCH ?")
        params.append(_fts_phrase(contains))
    if host:
        where.append("logs.host LIKE ?")
        params.append(f"%{host}%")
    if since is not None:
        where.append("logs.ts_us >= ?")
        params.append(_to_us(since))
    if until is not None:
        where.append("logs.ts_us < ?")
        params.append(_to_us(until))
    if severity:
        where.append("logs.severity = ?")
        params.append(severity.lower())
    if min_severity:
        allowed = severities_at_or_above(min_severity.lower())
        where.append(f"logs.severity IN ({','.join('?' * len(allowed))})")
        params.extend(allowed)
    if source:
        if source.lower() == SYSLOG_SOURCE_FAMILY:
            # Reserved family: device syslog only — EXCLUDE every declared controller tag. Match
            # whole csv tokens (comma-bracketed, the collector-merge idiom in `_insert_event`) so a
            # host whose name merely contains a family word ("unifi-annex") is never misclassified.
            for tag in sorted(CONTROLLER_COLLECTOR_TAGS):
                where.append("',' || logs.collectors || ',' NOT LIKE ?")
                params.append(f"%,{tag},%")
        else:
            # Every other value keeps its pre-050 case-insensitive substring semantics (FR-003).
            where.append("logs.collectors LIKE ?")
            params.append(f"%{source}%")
    return join, where, params


def query_events(
    conn: sqlite3.Connection,
    *,
    host: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    severity: str | None = None,
    min_severity: str | None = None,
    contains: str | None = None,
    source: str | None = None,
    limit: int = 200,
) -> list[LogEvent]:
    """Read matching events newest-first, bounded by ``limit``. Read-only (SELECT only).

    ``source`` is a case-insensitive substring match on the ``collectors`` tag — e.g. ``"unifi"``
    for UniFi controller events (feature 016) or a syslog-ng collector name for RouterOS — so a
    UniFi site name that collides with a RouterOS host (``--host Oak`` vs ``oak-*``) can be
    disambiguated. The reserved value ``"syslog"`` (feature 050, case-insensitive) is the one
    exception: it selects device syslog only (rows with no declared controller tag; see
    :data:`CONTROLLER_COLLECTOR_TAGS`), not a substring. ``min_severity`` (038) is a floor: warning
    ⇒ warning/error/critical.
    """

    join, where, params = _filter_clauses(
        host=host, since=since, until=until, severity=severity,
        min_severity=min_severity, contains=contains, source=source,
    )
    sql = (
        "SELECT logs.ts_iso, logs.host, logs.subsystem, logs.topics, logs.severity, "
        "logs.message, logs.raw_line, logs.collectors FROM logs" + join
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY logs.ts_us DESC LIMIT ?"
    params.append(limit)

    events: list[LogEvent] = []
    for row in conn.execute(sql, params):
        events.append(_row_to_event(row))
    return events


def _row_to_event(row: sqlite3.Row) -> LogEvent:
    """Build a :class:`LogEvent` from a ``logs`` row (column access by name)."""

    return LogEvent(
        ts=datetime.fromisoformat(row["ts_iso"]),
        host=row["host"],
        topics=row["topics"],
        message=row["message"],
        subsystem=row["subsystem"],
        severity=row["severity"],
        raw_line=row["raw_line"],
        collectors=row["collectors"].split(",") if row["collectors"] else [],
    )


# --- watcher cursor / watermark (feature 014) -------------------------------------------------
#
# The watermark is the monotonic ingest `id` (an autoincrement primary key), NOT the event
# timestamp. `id` reflects ingest order, so it is immune to device clock skew — a router with an
# unsynced clock that logs a far-future timestamp cannot push the watermark past real events and
# blind the watcher (a failure mode seen against the live store, where some PtP links report dates
# days in the future).


def latest_cursor(conn: sqlite3.Connection) -> int | None:
    """Return the newest event ``id`` (the watermark), or ``None`` if the store is empty."""

    row = conn.execute("SELECT MAX(id) AS m FROM logs").fetchone()
    value = row["m"] if row is not None else None
    return int(value) if value is not None else None


def query_since(
    conn: sqlite3.Connection,
    *,
    after_id: int,
    limit: int,
    host: str | None = None,
    min_severity: str | None = None,
    contains: str | None = None,
    source: str | None = None,
) -> list[tuple[int, LogEvent]]:
    """Events ingested after ``after_id`` (the watermark), in ingest order, bounded.

    Returns ``(id, event)`` pairs so the caller can advance the watermark to the last id it
    processed. Ordered by ``id`` (ingest order), independent of any device clock. Read-only.
    The optional filters (038: the web tail) share :func:`_filter_clauses` with
    :func:`query_events` — including the reserved ``source="syslog"`` family (050) — so the tail can
    never drift from the initial page; the bare call — the 014 watcher's — is unchanged.
    """

    join, where, params = _filter_clauses(
        host=host, since=None, until=None, severity=None,
        min_severity=min_severity, contains=contains, source=source,
    )
    sql = (
        "SELECT logs.id, logs.ts_iso, logs.host, logs.subsystem, logs.topics, logs.severity, "
        "logs.message, logs.raw_line, logs.collectors FROM logs" + join
        + " WHERE logs.id > ?"
    )
    query_params: list[object] = [after_id, *params]
    if where:
        sql += " AND " + " AND ".join(where)
    sql += " ORDER BY logs.id ASC LIMIT ?"
    query_params.append(limit)
    out: list[tuple[int, LogEvent]] = []
    for row in conn.execute(sql, query_params):
        out.append((int(row["id"]), _row_to_event(row)))
    return out


# --- watcher judge-window candidate query (feature 062, A1) -----------------------------------
#
# The judge window SELECTs only CANDIDATE events — those that survive every configured exclusion —
# BEFORE the LIMIT, so ignorable noise (obi's own login churn, ignored topics, muted collectors)
# can never consume the window budget and push the watcher behind (the July-2026 incident). The
# exclusion WHERE mirrors ``watch._filter_window`` rule-for-rule; watch.py keeps that Python filter
# as an in-pipeline backstop (research D2), and a differential test proves parity (FR-005).


@dataclass(frozen=True)
class WindowExclusions:
    """Filter values watch.py passes so SQL never leaves the logstore quarantine (Principle IV).

    Plain values only — no SQL: ``ignore_topics`` token-subset patterns (013/039 semantics),
    ``ignore_collectors`` whole-collector names (039), ``allowed_hosts`` (lowercased RouterOS
    identities+addresses, or ``None`` when ``routeros_only`` is off; an EMPTY set means the gate
    admits only unifi/uisp rows), and ``uisp_ignore_names`` substring excludes on uisp rows (035).
    """

    ignore_topics: tuple[str, ...] = ()
    ignore_collectors: tuple[str, ...] = ()
    allowed_hosts: frozenset[str] | None = None
    uisp_ignore_names: tuple[str, ...] = ()
    # 135: maintenance — a host whose lowercase form contains a site as a whole ``-`` token, or
    # equals one of the exact hosts (devices, UISP-flagged identities, declared UISP sites).
    maintenance_sites: tuple[str, ...] = ()
    maintenance_hosts: tuple[str, ...] = ()


def _like_literal(value: str) -> str:
    r"""Escape ``%`` ``_`` ``\`` so a value matches literally under ``LIKE … ESCAPE '\'``.

    Operator config values (a collector/topic/name) are always matched literally — a ``%`` or ``_``
    in a value is never a wildcard (FR-005 edge case)."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _window_exclusion_clauses(exclusions: WindowExclusions) -> tuple[list[str], list[object]]:
    """Build the candidate WHERE fragments + params mirroring ``watch._filter_window``.

    Each fragment is an ``AND`` term; a row is a candidate iff all pass. The comma-bracket token
    idiom (``',' || col || ','`` LIKE ``'%,tok,%'``) matches whole csv tokens — the house pattern
    used by the collector merge (013) and the syslog family filter (050) — so a value that merely
    contains a token as a substring is never mis-matched.
    """

    clauses: list[str] = []
    params: list[object] = []

    # 039: drop whole ignored collectors (case-insensitive whole-token match), before the gate.
    for name in exclusions.ignore_collectors:
        tok = name.strip().lower()
        if not tok:
            continue
        clauses.append("',' || logs.collectors || ',' NOT LIKE ? ESCAPE '\\'")
        params.append(f"%,{_like_literal(tok)},%")

    # routeros_only curated-source gate: keep a row that is a RouterOS host OR a unifi/uisp row.
    if exclusions.allowed_hosts is not None:
        host_terms: list[str] = []
        if exclusions.allowed_hosts:
            placeholders = ",".join("?" * len(exclusions.allowed_hosts))
            host_terms.append(f"lower(logs.host) IN ({placeholders})")
            params.extend(sorted(exclusions.allowed_hosts))
        host_terms.append("',' || logs.collectors || ',' LIKE '%,unifi,%'")
        host_terms.append("',' || logs.collectors || ',' LIKE '%,uisp,%'")
        clauses.append("(" + " OR ".join(host_terms) + ")")

    # ignore_topics: drop a row whose topics contain ALL of some pattern's tokens (token-subset).
    for pattern in exclusions.ignore_topics:
        toks = [t.strip().lower() for t in pattern.split(",") if t.strip()]
        if not toks:
            continue
        inner = " AND ".join("',' || logs.topics || ',' LIKE ? ESCAPE '\\'" for _ in toks)
        clauses.append(f"NOT ({inner})")
        params.extend(f"%,{_like_literal(t)},%" for t in toks)

    # 035: drop a uisp-collector row whose host substring-matches an ignore name.
    names = [n.strip().lower() for n in exclusions.uisp_ignore_names if n.strip()]
    if names:
        host_like = " OR ".join("lower(logs.host) LIKE ? ESCAPE '\\'" for _ in names)
        clauses.append(
            "NOT (',' || logs.collectors || ',' LIKE '%,uisp,%' AND (" + host_like + "))"
        )
        params.extend(f"%{_like_literal(n)}%" for n in names)

    # 135: maintenance hosts (Maintenance.log_host_terms — the same rule set as the backstop).
    exact = [h.strip().lower() for h in exclusions.maintenance_hosts if h.strip()]
    if exact:
        clauses.append(f"lower(logs.host) NOT IN ({','.join('?' * len(exact))})")
        params.extend(exact)
    for site in (s.strip().lower() for s in exclusions.maintenance_sites):
        if not site:
            continue
        lit = _like_literal(site)
        clauses.append(
            "NOT (lower(logs.host) = ? OR lower(logs.host) LIKE ? ESCAPE '\\' "
            "OR lower(logs.host) LIKE ? ESCAPE '\\' OR lower(logs.host) LIKE ? ESCAPE '\\')"
        )
        params.extend([site, f"{lit}-%", f"%-{lit}", f"%-{lit}-%"])

    return clauses, params


def query_watch_candidates(
    conn: sqlite3.Connection,
    *,
    after_id: int,
    upto_id: int,
    limit: int,
    exclusions: WindowExclusions,
) -> list[tuple[int, LogEvent]]:
    """Candidate events in ``(after_id, upto_id]`` surviving every exclusion, id-ordered, bounded.

    Read-only. The window budget (``limit``) therefore counts CANDIDATES, not noise. Bounding at
    ``upto_id`` (the newest id snapshotted at scan start) makes the cursor's "advance to upto" rule
    race-free: rows ingested mid-run are outside the scanned range by construction (FR-002).
    """

    clauses, params = _window_exclusion_clauses(exclusions)
    sql = (
        "SELECT logs.id, logs.ts_iso, logs.host, logs.subsystem, logs.topics, logs.severity, "
        "logs.message, logs.raw_line, logs.collectors FROM logs "
        "WHERE logs.id > ? AND logs.id <= ?"
    )
    query_params: list[object] = [after_id, upto_id, *params]
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY logs.id ASC LIMIT ?"
    query_params.append(limit)
    out: list[tuple[int, LogEvent]] = []
    for row in conn.execute(sql, query_params):
        out.append((int(row["id"]), _row_to_event(row)))
    return out


def watch_window_stats(
    conn: sqlite3.Connection,
    *,
    after_id: int,
    upto_id: int,
    exclusions: WindowExclusions,
) -> tuple[int, int | None]:
    """Candidate ``(count, oldest_ts_us)`` over ``(after_id, upto_id]``. Read-only.

    Used after the cursor advance to measure how far behind the watcher is (A4, FR-010). ``oldest``
    is ``None`` when no candidate remains. Counting over the pk range is exact and cheap — immune to
    id gaps, unlike ``MAX(id) − cursor``.
    """

    clauses, params = _window_exclusion_clauses(exclusions)
    sql = (
        "SELECT COUNT(*) AS n, MIN(logs.ts_us) AS oldest FROM logs "
        "WHERE logs.id > ? AND logs.id <= ?"
    )
    query_params: list[object] = [after_id, upto_id, *params]
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    row = conn.execute(sql, query_params).fetchone()
    if row is None:
        return 0, None
    oldest = row["oldest"]
    return int(row["n"]), (int(oldest) if oldest is not None else None)


def newest_event(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """The newest stored event as ``(id, ts_us)``, or ``None`` if the store is empty. Read-only.

    One row carries both the scan bound (``id``) and ingest freshness (``ts_us``) for A4."""

    row = conn.execute("SELECT id, ts_us FROM logs ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return int(row["id"]), int(row["ts_us"])
