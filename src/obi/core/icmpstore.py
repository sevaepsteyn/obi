"""Local SQLite state store for ICMP reachability (feature 072).

A tiny companion to the feature-021 metrics store: where ``metricstore`` keeps a time-series of SNMP
readings, this keeps only the **current** up/down state of each monitored device — one row per
device, rewritten each ``obi icmp run`` cycle. No history: reachability is a point-in-time fact, and
the RTT/loss time-series is a deferred follow-on. The ``*/5`` collector writes it; ``obi icmp
status`` and the ``[watch.icmp]`` pass read it (read-only, via WAL, so a reader never blocks the
collector).

Like ``logstore``/``metricstore``, this is one of the few modules permitted to import ``sqlite3``
(Constitution Principle IV): the schema, upsert, read, and prune all live here so the collector and
CLI stay thin. Rows are vendor-neutral — a device identity, an address, and its reachability.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from obi.core.storebase import apply_schema

# sqlite3 stays quarantined here (the logstore/metricstore rule): callers catch StoreError.
StoreError = sqlite3.Error

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_state (
    device             TEXT    PRIMARY KEY,   -- roster IDENTITY (or the Siklu host), the stable key
    address            TEXT    NOT NULL,       -- probed address (an IP or a Siklu DNS name)
    up                 INTEGER NOT NULL,       -- 1 = reachable at the last evaluation, 0 = down
    consecutive_misses INTEGER NOT NULL,       -- missed cycles in a row (debounce for down_after)
    last_seen_us       INTEGER,                -- last successful reply, epoch µs UTC (NULL = never)
    last_change_us     INTEGER NOT NULL,       -- when `up` last flipped, epoch µs UTC
    updated_us         INTEGER NOT NULL        -- last probe of this device, epoch µs UTC
);
"""

# feature-059 pattern: ordered, run-once upgrades ON TOP OF the frozen v1 ``_SCHEMA``. Append here
# for every schema change after v1; never edit ``_SCHEMA``.
_MIGRATIONS: tuple[str, ...] = (
    # v1 -> v2 (feature 095): record the probe's round-trip average beside up/down, so the
    # dashboard's fleet tile shows latency without a second store. NULL = no measured reply yet
    # (a v1 row, a down device, or an unparseable ping output).
    "ALTER TABLE device_state ADD COLUMN last_rtt_us INTEGER",
)


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the store read-write for the collector, creating it + its parent with the schema."""

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    apply_schema(conn, _SCHEMA, _MIGRATIONS)
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the store read-only (``mode=ro``) for the CLI / watch pass — never creates or writes."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class DeviceState:
    """One device's current reachability. ``last_seen_us`` is ``None`` if it never replied.

    ``last_rtt_us`` (schema v2, feature 095) is the most recent probe's round-trip average —
    ``None`` for a v1 row, a down device, or an output the prober could not parse.
    """

    device: str
    address: str
    up: bool
    consecutive_misses: int
    last_seen_us: int | None
    last_change_us: int
    updated_us: int
    last_rtt_us: int | None = None


def upsert_state(conn: sqlite3.Connection, rows: Iterable[DeviceState]) -> None:
    """Insert-or-replace each device's current state (PRIMARY KEY on ``device``). Caller commits."""

    conn.executemany(
        "INSERT INTO device_state "
        "(device, address, up, consecutive_misses, last_seen_us, last_change_us, updated_us, "
        "last_rtt_us) "
        "VALUES (:device, :address, :up, :misses, :last_seen_us, :last_change_us, :updated_us, "
        ":last_rtt_us) "
        "ON CONFLICT(device) DO UPDATE SET "
        "address=excluded.address, up=excluded.up, "
        "consecutive_misses=excluded.consecutive_misses, last_seen_us=excluded.last_seen_us, "
        "last_change_us=excluded.last_change_us, updated_us=excluded.updated_us, "
        "last_rtt_us=excluded.last_rtt_us",
        [
            {
                "device": r.device,
                "address": r.address,
                "up": 1 if r.up else 0,
                "misses": r.consecutive_misses,
                "last_seen_us": r.last_seen_us,
                "last_change_us": r.last_change_us,
                "updated_us": r.updated_us,
                "last_rtt_us": r.last_rtt_us,
            }
            for r in rows
        ],
    )


def load_states(conn: sqlite3.Connection) -> list[DeviceState]:
    """Every device's current state, sorted by device identity."""

    try:
        cur = conn.execute(
            "SELECT device, address, up, consecutive_misses, last_seen_us, last_change_us, "
            "updated_us, last_rtt_us FROM device_state ORDER BY device"
        )
    except sqlite3.OperationalError:
        # A v1 store opened read-only (mode=ro never migrates): the collector's next connect()
        # applies the v2 migration; until then readers see rtt as NULL rather than erroring.
        cur = conn.execute(
            "SELECT device, address, up, consecutive_misses, last_seen_us, last_change_us, "
            "updated_us, NULL FROM device_state ORDER BY device"
        )
    return [
        DeviceState(
            device=str(row[0]),
            address=str(row[1]),
            up=bool(row[2]),
            consecutive_misses=int(row[3]),
            last_seen_us=int(row[4]) if row[4] is not None else None,
            last_change_us=int(row[5]),
            updated_us=int(row[6]),
            last_rtt_us=int(row[7]) if row[7] is not None else None,
        )
        for row in cur.fetchall()
    ]


def prune_absent(conn: sqlite3.Connection, keep_devices: Iterable[str]) -> int:
    """Delete rows for devices not in ``keep_devices`` (decommissioned / out of inventory).

    Returns the number of rows removed. Caller commits. An empty ``keep_devices`` clears the store.
    """

    keep = list(keep_devices)
    placeholders = ",".join("?" for _ in keep)
    if not keep:
        cur = conn.execute("DELETE FROM device_state")
    else:
        cur = conn.execute(
            f"DELETE FROM device_state WHERE device NOT IN ({placeholders})", keep
        )
    return cur.rowcount
