"""Local SQLite store for mesh position history (feature 117).

The durable memory behind "where was Tumble last?": every position fix obi passively sees
for a ``report_only`` mesh node — from the node-table reads the locate engine already
performs and from the ``obi mesh record`` sweep — lands here as one immutable row. The
radio's own node table caps at ~120 entries and evicts the longest-quiet node (the
2026-08-30 incident: the tracker's last-known fix was lost with it); this store is what
survives that, radio resets, and hardware swaps.

The icmp/weather/traffic small-store shape: ``storebase.apply_schema`` on WAL, ``sqlite3``
quarantined here (callers catch :data:`StoreError`). Coordinates are stored as the
Meshtastic wire integers (degrees × 1e7) so the dedupe key compares exactly; a device
that stamped no fix time stores **0** (never NULL — SQLite UNIQUE treats NULLs as
distinct, which would defeat dedupe for precisely the clock-less fixes that repeat most).
Writes are ``INSERT OR IGNORE``: concurrent observers of the same new fix converge on one
row without locks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from obi.core.storebase import apply_schema

# sqlite3 stays quarantined here (the logstore/metricstore rule): callers catch StoreError.
StoreError = sqlite3.Error

#: A device fix time is believed only when it is positive and no further ahead of the
#: observation clock than this (LoRa replies land in seconds; anything beyond is a bad clock).
_FORWARD_SKEW_S = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_fix (
    id            INTEGER PRIMARY KEY,
    node_id       TEXT    NOT NULL,       -- Meshtastic node id (!1234abcd), the stable identity
    lat_udeg      INTEGER NOT NULL,       -- latitude  x 1e7 (wire format: exact equality)
    lon_udeg      INTEGER NOT NULL,       -- longitude x 1e7
    altitude_m    INTEGER,                -- device-reported, when present
    fix_time_s    INTEGER NOT NULL,       -- device-claimed fix epoch seconds; 0 = none given
    observed_at_s INTEGER NOT NULL,       -- obi's clock when the fix was read from the radio
    hops_away     INTEGER,                -- heard metadata at observation (NULL = unknown)
    snr_db        REAL,                   -- heard metadata at observation
    address       TEXT    NOT NULL        -- BRC address rendered at record time
);
CREATE UNIQUE INDEX IF NOT EXISTS position_fix_dedupe
    ON position_fix (node_id, fix_time_s, lat_udeg, lon_udeg);
CREATE INDEX IF NOT EXISTS position_fix_node_time
    ON position_fix (node_id, observed_at_s);
"""

# feature-059 pattern: ordered, run-once upgrades ON TOP OF the frozen v1 ``_SCHEMA``.
_MIGRATIONS: tuple[str, ...] = ()

#: The effective-instant expression, shared by last_known (DESC) and track (ASC): a sane
#: device fix time wins, else the observation clock. Kept in SQL so ordering and range
#: filtering happen in the index scan, mirrored by :func:`effective_fix_s` for Python-side use.
_EFFECTIVE_SQL = (
    "CASE WHEN fix_time_s > 0 AND fix_time_s <= observed_at_s + "
    f"{_FORWARD_SKEW_S} THEN fix_time_s ELSE observed_at_s END"
)


@dataclass(frozen=True)
class RecordedFix:
    """One immutable observed position (spec FR-003). Coordinates in degrees for callers;
    the wire-integer form exists only inside the store."""

    node_id: str
    latitude: float
    longitude: float
    altitude_m: int | None
    fix_time_s: int  # 0 = the device gave no fix time
    observed_at_s: int
    hops_away: int | None
    snr_db: float | None
    address: str

    @property
    def effective_fix_s(self) -> int:
        return effective_fix_s(self.fix_time_s, self.observed_at_s)


def effective_fix_s(fix_time_s: int, observed_at_s: int) -> int:
    """The instant a fix is honestly dated to: the device's own fix time when sane
    (positive, not further than the skew allowance ahead of the observation clock),
    else the observation clock — the "garbage device clock" rule from the spec."""

    if 0 < fix_time_s <= observed_at_s + _FORWARD_SKEW_S:
        return fix_time_s
    return observed_at_s


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the store read-write, creating it + its parent with the schema."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    apply_schema(conn, _SCHEMA, _MIGRATIONS)
    conn.commit()
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the store read-only (``mode=ro``) for answer-time lookups — never creates."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def record_fix(conn: sqlite3.Connection, fix: RecordedFix) -> bool:
    """Insert one observed fix; ``True`` = a new row, ``False`` = already recorded.

    ``INSERT OR IGNORE`` against the dedupe key makes re-observation and concurrent
    observers converge on one row (SC-004); rows are never updated.
    """

    cursor = conn.execute(
        "INSERT OR IGNORE INTO position_fix "
        "(node_id, lat_udeg, lon_udeg, altitude_m, fix_time_s, observed_at_s,"
        " hops_away, snr_db, address) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fix.node_id,
            round(fix.latitude * 1e7),
            round(fix.longitude * 1e7),
            fix.altitude_m,
            fix.fix_time_s,
            fix.observed_at_s,
            fix.hops_away,
            fix.snr_db,
            fix.address,
        ),
    )
    conn.commit()
    return cursor.rowcount == 1


def _row_to_fix(row: tuple) -> RecordedFix:  # type: ignore[type-arg]
    (node_id, lat_udeg, lon_udeg, altitude_m, fix_time_s, observed_at_s, hops, snr, address) = row
    return RecordedFix(
        node_id=str(node_id),
        latitude=int(lat_udeg) * 1e-7,
        longitude=int(lon_udeg) * 1e-7,
        altitude_m=int(altitude_m) if altitude_m is not None else None,
        fix_time_s=int(fix_time_s),
        observed_at_s=int(observed_at_s),
        hops_away=int(hops) if hops is not None else None,
        snr_db=float(snr) if snr is not None else None,
        address=str(address),
    )


_COLUMNS = (
    "node_id, lat_udeg, lon_udeg, altitude_m, fix_time_s, observed_at_s, hops_away, snr_db, address"
)


def last_known(conn: sqlite3.Connection, node_id: str) -> RecordedFix | None:
    """The most recent recorded fix for ``node_id`` by effective instant; ``None`` = no history."""

    row = conn.execute(
        f"SELECT {_COLUMNS} FROM position_fix WHERE node_id = ? "
        f"ORDER BY {_EFFECTIVE_SQL} DESC, observed_at_s DESC, id DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    return _row_to_fix(tuple(row)) if row is not None else None


def track(
    conn: sqlite3.Connection, node_id: str, *, since_s: int, until_s: int
) -> list[RecordedFix]:
    """The time-ordered track: fixes whose effective instant lies in [since_s, until_s]."""

    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM position_fix WHERE node_id = ? "
        f"AND {_EFFECTIVE_SQL} BETWEEN ? AND ? "
        f"ORDER BY {_EFFECTIVE_SQL} ASC, observed_at_s ASC, id ASC",
        (node_id, since_s, until_s),
    ).fetchall()
    return [_row_to_fix(tuple(row)) for row in rows]
