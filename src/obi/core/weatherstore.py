"""Local SQLite store for Black Rock City weather snapshots (feature 110).

The hourly ``obi weather run`` collector writes one **snapshot** per cycle — the fetch
timestamp, per-source success/failure, the current conditions, the 24 hourly forecast rows,
and the active NWS alerts at capture time. The ``weather_forecast`` chat tool and
``obi weather status`` read it (read-only, via WAL, so a reader never blocks the collector).
The store IS the feature's degraded mode (Principle VI): with the uplink down the last
committed snapshot keeps answering, with its age; a failed cycle still leaves a snapshot
row, because the failure record is evidence, not noise.

Like ``icmpstore``/``metricstore``, this is one of the few modules permitted to import
``sqlite3`` (Constitution Principle IV): schema, insert, prune, and the newest/nearest/
coverage queries all live here so the collector, CLI, and tool handler stay thin.
Timestamps are epoch microseconds UTC (``*_us``); rendering converts to playa-local.
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
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY,
    fetched_us      INTEGER NOT NULL,   -- cycle wall-clock, epoch µs UTC (the age anchor)
    forecast_ok     INTEGER NOT NULL,   -- 1 = Open-Meteo fetched AND parsed
    forecast_error  TEXT,               -- terse classification when forecast_ok = 0
    alerts_ok       INTEGER NOT NULL,   -- 1 = NWS alerts fetched AND parsed
    alerts_error    TEXT,
    -- current conditions at fetch time (all NULL when forecast_ok = 0; an individual NULL
    -- means the source did not provide the value — rendered "unavailable", never 0)
    temp_f          REAL,
    humidity_pct    REAL,
    dew_point_f     REAL,
    precip_in       REAL,
    wind_mph        REAL,
    wind_dir_deg    REAL,
    gust_mph        REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_fetched ON snapshots (fetched_us);

CREATE TABLE IF NOT EXISTS hourly (
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots (id),
    hour_us         INTEGER NOT NULL,   -- forecast valid hour, epoch µs UTC
    temp_f          REAL,
    humidity_pct    REAL,
    dew_point_f     REAL,
    precip_in       REAL,
    precip_prob_pct REAL,
    wind_mph        REAL,
    wind_dir_deg    REAL,
    gust_mph        REAL,
    UNIQUE (snapshot_id, hour_us)
);

CREATE TABLE IF NOT EXISTS alerts (
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots (id),
    event           TEXT NOT NULL,      -- NWS event name, e.g. "High Wind Warning"
    severity        TEXT,               -- as issued (Minor/Moderate/Severe/Extreme)
    onset_us        INTEGER,            -- effective window, epoch µs UTC (nullable as issued)
    ends_us         INTEGER,
    headline        TEXT                -- NWS headline, served verbatim
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
class CurrentConditions:
    """The source's current block at fetch time. ``None`` = value not provided, never zero."""

    temp_f: float | None
    humidity_pct: float | None
    dew_point_f: float | None
    precip_in: float | None
    wind_mph: float | None
    wind_dir_deg: float | None
    gust_mph: float | None


@dataclass(frozen=True)
class HourlyRow:
    """One forecast hour of one snapshot. ``hour_us`` is the valid hour, epoch µs UTC."""

    hour_us: int
    temp_f: float | None
    humidity_pct: float | None
    dew_point_f: float | None
    precip_in: float | None
    precip_prob_pct: float | None
    wind_mph: float | None
    wind_dir_deg: float | None
    gust_mph: float | None


@dataclass(frozen=True)
class StoredAlert:
    """One active NWS alert as issued (event/severity/window/headline served verbatim)."""

    event: str
    severity: str | None
    onset_us: int | None
    ends_us: int | None
    headline: str | None


@dataclass(frozen=True)
class Snapshot:
    """One collector cycle's outcome. ``current`` is ``None`` when the forecast fetch failed."""

    id: int
    fetched_us: int
    forecast_ok: bool
    forecast_error: str | None
    alerts_ok: bool
    alerts_error: str | None
    current: CurrentConditions | None


def insert_cycle(
    conn: sqlite3.Connection,
    *,
    fetched_us: int,
    forecast_ok: bool,
    forecast_error: str | None,
    current: CurrentConditions | None,
    hourly: Sequence[HourlyRow],
    alerts_ok: bool,
    alerts_error: str | None,
    alerts: Sequence[StoredAlert],
) -> int:
    """Insert one cycle's snapshot + hourly rows + alerts; return the snapshot id.

    Always inserts a snapshot — a total-failure cycle (both ``*_ok`` false) is the durable
    degraded-mode evidence. Does NOT commit — the caller commits so a whole cycle (insert +
    prune) is one atomic transaction (the metricstore rule).
    """

    cur = conn.execute(
        "INSERT INTO snapshots (fetched_us, forecast_ok, forecast_error, alerts_ok, "
        "alerts_error, temp_f, humidity_pct, dew_point_f, precip_in, wind_mph, "
        "wind_dir_deg, gust_mph) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fetched_us,
            1 if forecast_ok else 0,
            forecast_error,
            1 if alerts_ok else 0,
            alerts_error,
            current.temp_f if current else None,
            current.humidity_pct if current else None,
            current.dew_point_f if current else None,
            current.precip_in if current else None,
            current.wind_mph if current else None,
            current.wind_dir_deg if current else None,
            current.gust_mph if current else None,
        ),
    )
    snapshot_id = int(cur.lastrowid or 0)
    conn.executemany(
        "INSERT OR IGNORE INTO hourly (snapshot_id, hour_us, temp_f, humidity_pct, "
        "dew_point_f, precip_in, precip_prob_pct, wind_mph, wind_dir_deg, gust_mph) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                snapshot_id,
                r.hour_us,
                r.temp_f,
                r.humidity_pct,
                r.dew_point_f,
                r.precip_in,
                r.precip_prob_pct,
                r.wind_mph,
                r.wind_dir_deg,
                r.gust_mph,
            )
            for r in hourly
        ],
    )
    conn.executemany(
        "INSERT INTO alerts (snapshot_id, event, severity, onset_us, ends_us, headline) "
        "VALUES (?,?,?,?,?,?)",
        [(snapshot_id, a.event, a.severity, a.onset_us, a.ends_us, a.headline) for a in alerts],
    )
    return snapshot_id


def prune(conn: sqlite3.Connection, *, older_than_us: int) -> int:
    """Delete snapshots with ``fetched_us < older_than_us`` and their children; return the
    number of snapshots removed. Caller commits. Children are deleted explicitly (SQLite
    only honors REFERENCES with a per-connection pragma; the store does not depend on it)."""

    conn.execute(
        "DELETE FROM hourly WHERE snapshot_id IN "
        "(SELECT id FROM snapshots WHERE fetched_us < ?)",
        (older_than_us,),
    )
    conn.execute(
        "DELETE FROM alerts WHERE snapshot_id IN "
        "(SELECT id FROM snapshots WHERE fetched_us < ?)",
        (older_than_us,),
    )
    cur = conn.execute("DELETE FROM snapshots WHERE fetched_us < ?", (older_than_us,))
    return cur.rowcount


_SNAPSHOT_COLS = (
    "id, fetched_us, forecast_ok, forecast_error, alerts_ok, alerts_error, "
    "temp_f, humidity_pct, dew_point_f, precip_in, wind_mph, wind_dir_deg, gust_mph"
)


def _snapshot_from_row(row: sqlite3.Row | tuple[object, ...]) -> Snapshot:
    forecast_ok = bool(row[2])
    current = (
        CurrentConditions(
            temp_f=_opt_float(row[6]),
            humidity_pct=_opt_float(row[7]),
            dew_point_f=_opt_float(row[8]),
            precip_in=_opt_float(row[9]),
            wind_mph=_opt_float(row[10]),
            wind_dir_deg=_opt_float(row[11]),
            gust_mph=_opt_float(row[12]),
        )
        if forecast_ok
        else None
    )
    return Snapshot(
        id=int(row[0]),  # type: ignore[arg-type]
        fetched_us=int(row[1]),  # type: ignore[arg-type]
        forecast_ok=forecast_ok,
        forecast_error=str(row[3]) if row[3] is not None else None,
        alerts_ok=bool(row[4]),
        alerts_error=str(row[5]) if row[5] is not None else None,
        current=current,
    )


def _opt_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _one_snapshot(conn: sqlite3.Connection, where_order: str, params: tuple[object, ...]) -> (
    Snapshot | None
):
    row = conn.execute(
        f"SELECT {_SNAPSHOT_COLS} FROM snapshots {where_order} LIMIT 1", params
    ).fetchone()
    return _snapshot_from_row(row) if row is not None else None


def newest_snapshot(conn: sqlite3.Connection) -> Snapshot | None:
    """The most recent cycle's snapshot (whatever its per-source outcome), or ``None``."""

    return _one_snapshot(conn, "ORDER BY fetched_us DESC, id DESC", ())


def newest_snapshot_with_forecast(conn: sqlite3.Connection) -> Snapshot | None:
    """The most recent snapshot whose forecast fetch succeeded — the fallback the outlook
    serves when the newest cycle's forecast source failed."""

    return _one_snapshot(
        conn, "WHERE forecast_ok = 1 ORDER BY fetched_us DESC, id DESC", ()
    )


def nearest_snapshot(conn: sqlite3.Connection, at_us: int) -> Snapshot | None:
    """The forecast-bearing snapshot recorded nearest ``at_us`` (history mode). Failed-fetch
    snapshots are skipped — they recorded no conditions to serve."""

    return _one_snapshot(
        conn,
        "WHERE forecast_ok = 1 ORDER BY ABS(fetched_us - ?), id DESC",
        (at_us,),
    )


def hourly_rows(conn: sqlite3.Connection, snapshot_id: int) -> list[HourlyRow]:
    """One snapshot's forecast rows, in hour order."""

    cur = conn.execute(
        "SELECT hour_us, temp_f, humidity_pct, dew_point_f, precip_in, precip_prob_pct, "
        "wind_mph, wind_dir_deg, gust_mph FROM hourly WHERE snapshot_id = ? ORDER BY hour_us",
        (snapshot_id,),
    )
    return [
        HourlyRow(
            hour_us=int(row[0]),
            temp_f=_opt_float(row[1]),
            humidity_pct=_opt_float(row[2]),
            dew_point_f=_opt_float(row[3]),
            precip_in=_opt_float(row[4]),
            precip_prob_pct=_opt_float(row[5]),
            wind_mph=_opt_float(row[6]),
            wind_dir_deg=_opt_float(row[7]),
            gust_mph=_opt_float(row[8]),
        )
        for row in cur.fetchall()
    ]


def alert_rows(conn: sqlite3.Connection, snapshot_id: int) -> list[StoredAlert]:
    """One snapshot's active alerts, as issued."""

    cur = conn.execute(
        "SELECT event, severity, onset_us, ends_us, headline FROM alerts "
        "WHERE snapshot_id = ? ORDER BY onset_us, event",
        (snapshot_id,),
    )
    return [
        StoredAlert(
            event=str(row[0]),
            severity=str(row[1]) if row[1] is not None else None,
            onset_us=int(row[2]) if row[2] is not None else None,
            ends_us=int(row[3]) if row[3] is not None else None,
            headline=str(row[4]) if row[4] is not None else None,
        )
        for row in cur.fetchall()
    ]


def coverage(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """(oldest, newest) snapshot ``fetched_us``, or ``None`` for an empty store — the
    history mode's honest "records begin here" boundary."""

    row = conn.execute("SELECT MIN(fetched_us), MAX(fetched_us) FROM snapshots").fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0]), int(row[1])
