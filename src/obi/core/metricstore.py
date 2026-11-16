"""Local SQLite time-series store for SNMP metrics (feature 021).

obi's **first persistent time-series store**. A scheduled collector (`obi metrics ingest`) polls
SNMP from the gateways and persists each **raw** numeric reading here; `obi metrics history` derives
rates from the stored counters at query time. Deliberately a *separate* database from the
feature-013 log store (`logstore.py`) — the schemas, write rates, and retention policies are
unrelated (logs never prune; metrics self-prune).

Like `logstore`, this is one of the few modules permitted to import ``sqlite3`` (Constitution
Principle IV): the schema, insert, retention prune, and the rate/bucket query all live here so the
engine and CLI stay thin. Rows are vendor-neutral (tagged by device identity / profile / metric);
nothing SNMP-specific leaks in beyond the metric names.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from obi.core.models import SeriesSummary
from obi.core.storebase import apply_schema

# --- schema -----------------------------------------------------------------------------------

# sqlite3 stays quarantined here (the logstore rule): callers catch StoreError.
StoreError = sqlite3.Error

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id       INTEGER PRIMARY KEY,
    ts_us    INTEGER NOT NULL,        -- collection time, epoch microseconds, UTC
    ts_iso   TEXT    NOT NULL,        -- tz-aware ISO-8601 for display
    device   TEXT    NOT NULL,        -- roster IDENTITY (the stable key), e.g. "core-gw-1"
    address  TEXT    NOT NULL,        -- queried address (provenance), e.g. "192.0.2.1"
    profile  TEXT    NOT NULL,        -- "interfaces" | "system"
    metric   TEXT    NOT NULL,        -- logical name: "ifHCInOctets", "cpuLoad", ...
    ifindex  TEXT    NOT NULL,        -- interface index; '' for device-scalar metrics
    ifname   TEXT,                    -- interface label, e.g. "ether1" (NULL for scalars)
    kind     TEXT    NOT NULL,        -- MetricKind: "counter64"|"counter"|"gauge"|"timeticks"|...
    value    INTEGER NOT NULL,        -- raw numeric value (exact 64-bit; absent readings skipped)
    UNIQUE (ts_us, device, metric, ifindex)
);
CREATE INDEX IF NOT EXISTS idx_samples_dev_metric_ts ON samples(device, metric, ts_us);
CREATE INDEX IF NOT EXISTS idx_samples_ts            ON samples(ts_us);
"""

# feature 059: ordered, run-once upgrades applied ON TOP OF the frozen v1 ``_SCHEMA`` above.
# ``_MIGRATIONS[i]`` takes the store from version ``i + 1`` to ``i + 2``. Append here for every
# schema change after v1 (e.g. ``"ALTER TABLE samples ADD COLUMN ..."``); never edit ``_SCHEMA``.
_MIGRATIONS: tuple[str, ...] = ()

# SQLite stores integers as signed 8-byte; a value outside this range raises on insert. Counter64
# only approaches 2**63 after ~9 exabytes on one interface without a reboot/wrap — practically
# unreachable — but the collector guards against it (models._SIGNED64) so a cycle never crashes.
_SIGNED64_MAX = 2**63 - 1

#: MetricKind values (stored in ``kind``) whose readings are monotonic counters → query as a rate.
_COUNTER_KINDS = frozenset({"counter", "counter64"})


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the store read-write for ingest, creating it + its parent with the schema applied."""

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


# --- insert -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredSample:
    """One row to persist — a single numeric reading. ``ifindex`` is ``''`` for device-scalars."""

    ts_us: int
    ts_iso: str
    device: str
    address: str
    profile: str
    metric: str
    ifindex: str
    ifname: str | None
    kind: str
    value: int


def insert_samples(conn: sqlite3.Connection, rows: Iterable[StoredSample]) -> int:
    """Insert readings idempotently (``INSERT OR IGNORE``); return the number of new rows.

    A row whose ``(ts_us, device, metric, ifindex)`` already exists is skipped (FR-021). Does NOT
    commit — the caller commits so a whole cycle (inserts + prune) is one atomic transaction.
    """

    inserted = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO samples "
            "(ts_us, ts_iso, device, address, profile, metric, ifindex, ifname, kind, value) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                r.ts_us,
                r.ts_iso,
                r.device,
                r.address,
                r.profile,
                r.metric,
                r.ifindex,
                r.ifname,
                r.kind,
                r.value,
            ),
        )
        inserted += cur.rowcount
    return inserted


def prune(conn: sqlite3.Connection, *, older_than_us: int) -> int:
    """Delete readings with ``ts_us < older_than_us`` (retention); return rows removed. Caller
    commits."""

    cur = conn.execute("DELETE FROM samples WHERE ts_us < ?", (older_than_us,))
    return cur.rowcount


# --- coverage (status) ------------------------------------------------------------------------


@dataclass
class DeviceCoverage:
    device: str
    readings: int
    newest_us: int | None


@dataclass
class Coverage:
    total_readings: int
    oldest_us: int | None
    newest_us: int | None
    devices: list[DeviceCoverage] = field(default_factory=list)


def coverage(conn: sqlite3.Connection) -> Coverage:
    """Summarize store coverage for ``obi metrics status``. Read-only; tolerates an empty store."""

    try:
        total, oldest, newest = conn.execute(
            "SELECT COUNT(*), MIN(ts_us), MAX(ts_us) FROM samples"
        ).fetchone()
        rows = conn.execute(
            "SELECT device, COUNT(*), MAX(ts_us) FROM samples GROUP BY device ORDER BY device"
        ).fetchall()
    except sqlite3.OperationalError:
        return Coverage(total_readings=0, oldest_us=None, newest_us=None)
    devices = [
        DeviceCoverage(
            device=str(r[0]), readings=int(r[1]), newest_us=int(r[2]) if r[2] is not None else None
        )
        for r in rows
    ]
    return Coverage(
        total_readings=int(total or 0),
        oldest_us=int(oldest) if oldest is not None else None,
        newest_us=int(newest) if newest is not None else None,
        devices=devices,
    )


# --- reachability (feature 030) ---------------------------------------------------------------


#: Infer a device's cadence from its most-recent N collection timestamps (feature 030).
_CADENCE_SAMPLES = 32


@dataclass(frozen=True)
class DeviceFreshness:
    """Per-device freshness snapshot for the reachability watch (feature 030)."""

    device: str
    address: str | None  # address of the device's newest sample (provenance); None if unknown
    newest_us: int  # epoch-µs of the device's most recent sample (over ALL its rows)
    readings: int  # recent cycles sampled for cadence (representative series, ≤ _CADENCE_SAMPLES)
    cadence_us: int | None  # median inter-cycle spacing over recent cycles; None if < 2 cycles


def _median_gap_us(ts_desc: list[int]) -> int | None:
    """Median gap (µs) between consecutive collection timestamps; ``None`` for fewer than two.

    ``ts_desc`` is a list of distinct timestamps (order irrelevant — sorted here). Mirrors the
    median-spacing cadence notion of :func:`_floor_seconds`, but returned in µs and per device.
    """

    if len(ts_desc) < 2:
        return None
    ordered = sorted(ts_desc)
    gaps = [t1 - t0 for t0, t1 in zip(ordered, ordered[1:], strict=False) if t1 > t0]
    if not gaps:
        return None
    return int(statistics.median(gaps))


def device_freshness(
    conn: sqlite3.Connection, *, now_us: int, exclude_metric_prefixes: Sequence[str] = ()
) -> list[DeviceFreshness]:
    """Per-device freshness for the reachability watch (feature 030). Read-only; tolerates empty.

    For **every** device present in the store (retention-bounded — no lookback window), reports its
    newest sample time, that sample's address, the number of recent cycles sampled, and its observed
    cadence (median inter-cycle spacing over its most-recent ``_CADENCE_SAMPLES`` cycles; ``None``
    when it has fewer than two). Cadence is drawn from the device's *own* recent samples, not a
    wall-clock window, so a device that went silent long ago keeps a defined cadence and stays
    evaluable until retention prunes it. ``now_us`` is accepted for caller symmetry; the query does
    not filter by it. Returns ``[]`` for an empty/missing store.

    Cadence is measured on a **single representative series** — the ``(metric, ifindex)`` with the
    most readings, which the collector writes once per cycle — not the raw distinct-``ts_us``.
    One cycle emits several timestamps a few seconds apart (the ``system`` and ``interfaces`` polls
    land separately), so a raw distinct-timestamp median would collapse to that intra-cycle jitter
    instead of the true inter-cycle cadence.

    ``exclude_metric_prefixes`` (feature 096, research D4): devices whose EVERY row matches an
    excluded metric prefix are dropped entirely — the reachability watch passes ``("linux:",)``
    so the SSH-probed collector VMs sharing this store are never adopted as SNMP devices (a
    broken probe must not fabricate an "unreachable" alert).
    """

    predicate = ""
    params: list[object] = []
    if exclude_metric_prefixes:
        predicate = " WHERE " + " AND ".join("metric NOT LIKE ?" for _ in exclude_metric_prefixes)
        params = [f"{p}%" for p in exclude_metric_prefixes]
    try:
        rows = conn.execute(
            f"SELECT device, MAX(ts_us) FROM samples{predicate} GROUP BY device ORDER BY device",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # no samples table yet — treat as empty (never crash the caller)

    result: list[DeviceFreshness] = []
    for device_row in rows:
        if device_row[1] is None:  # device with no timestamped rows (defensive)
            continue
        device = str(device_row[0])
        newest_us = int(device_row[1])
        addr_row = conn.execute(
            "SELECT address FROM samples WHERE device = ? AND ts_us = ? LIMIT 1",
            (device, newest_us),
        ).fetchone()
        address = str(addr_row[0]) if addr_row and addr_row[0] is not None else None

        # Representative series: the (metric, ifindex) the collector writes once per cycle, so its
        # inter-reading spacing IS the cadence (not the mixed distinct-ts stream — see docstring).
        rep = conn.execute(
            "SELECT metric, ifindex FROM samples WHERE device = ? "
            "GROUP BY metric, ifindex ORDER BY COUNT(*) DESC, metric, ifindex LIMIT 1",
            (device,),
        ).fetchone()
        cadence_us: int | None = None
        readings = 0
        if rep is not None:
            series = conn.execute(
                "SELECT ts_us FROM samples WHERE device = ? AND metric = ? AND ifindex = ? "
                "ORDER BY ts_us DESC LIMIT ?",
                (device, rep[0], rep[1], _CADENCE_SAMPLES),
            ).fetchall()
            ts = [int(x[0]) for x in series]
            readings = len(ts)
            cadence_us = _median_gap_us(ts)
        result.append(
            DeviceFreshness(
                device=device,
                address=address,
                newest_us=newest_us,
                readings=readings,
                cadence_us=cadence_us,
            )
        )
    return result


# --- query (history) --------------------------------------------------------------------------


@dataclass
class Bucket:
    """One downsampled bucket. ``avg/min/max`` are ``None`` for an empty (gap) bucket (``n==0``)."""

    start_iso: str
    avg: float | None
    min: float | None
    max: float | None
    n: int


@dataclass
class Series:
    """One interface's (or the device-scalar) series over the window. ``is_rate`` ⇒ per-second."""

    device: str
    metric: str
    ifname: str | None
    unit: str
    is_rate: bool
    buckets: list[Bucket]


def _unit_for(metric: str, *, is_rate: bool) -> str:
    """Display unit derived from the metric name (counter rates and the known gauges)."""

    lower = metric.lower()
    if not is_rate:
        if lower == "cpuload":
            return "%"
        # 096 precedence rule (data-model I1): exact-suffix branches match BEFORE the generic
        # words they contain — optics:supplyVoltageMv / optics:rxPowerMdbm carry "voltage" /
        # "power" inside a differently-scaled name.
        if lower.endswith("mdbm"):
            return "dBm"
        if lower.endswith("mv"):
            return "mV"
        if lower.endswith("ma"):
            return "mA"
        if lower.endswith("percent"):
            return "%"
        if lower.endswith("kb"):
            return "kB"
        if lower.endswith("speed"):
            return "rpm"
        if "temperature" in lower:
            return "°C"
        if "voltage" in lower:
            return "V"
        if "current" in lower:
            return "A"
        if "power" in lower or "consumption" in lower:
            return "W"
        return ""
    if "octet" in lower:
        return "bytes/s"
    if "error" in lower:
        return "errors/s"
    if "discard" in lower:
        return "discards/s"
    return "/s"


def _divisor_for(metric: str) -> float:
    """Query-time divisor turning a stored raw value into the unit :func:`_unit_for` declares.

    RouterOS's MIKROTIK-MIB reports ``mtxrHl*Temperature`` in **tenths** of a degree Celsius
    (deci-°C), so a 43 °C CPU is stored as ``430``. The store keeps the raw wire value (as it does
    for the interface counters); display scales by 10 so the number agrees with the ``°C`` unit
    already emitted. The 096 health/optics metrics follow the same raw-on-the-wire rule: dV/dA/dW
    sensor readings and ×10-normalized percentages divide by 10, optics power in milli-dBm divides
    by 1000 — with the exact-suffix branches checked BEFORE the generic words they contain (the
    data-model I1 precedence rule). Everything else is 1:1.
    """

    lower = metric.lower()
    if lower.endswith("mdbm"):
        return 1000.0
    if lower.endswith("mv") or lower.endswith("ma"):
        return 1.0
    if lower.endswith("percent"):
        return 10.0
    if "temperature" in lower:
        return 10.0
    if "voltage" in lower or "current" in lower:
        return 10.0
    if "power" in lower or "consumption" in lower:
        return 10.0
    return 1.0


def _points_for_series(rows: list[tuple[int, int]], *, is_rate: bool) -> list[tuple[int, float]]:
    """Turn ordered ``(ts_us, value)`` readings into ``(ts_us, y)`` points.

    Counter: each adjacent pair yields a per-second rate at the later timestamp; a decrease
    (reset/reboot/wrap) or a zero time delta skips that interval (FR-006). The first reading only
    seeds the next interval. Gauge: each reading is a point as-is.
    """

    if not is_rate:
        return [(ts, float(v)) for ts, v in rows]
    points: list[tuple[int, float]] = []
    for (t0, v0), (t1, v1) in zip(rows, rows[1:], strict=False):
        if t1 <= t0 or v1 < v0:  # gap-of-zero or counter reset → drop this interval
            continue
        points.append((t1, (v1 - v0) / ((t1 - t0) / 1_000_000)))
    return points


def _floor_seconds(rows: list[tuple[int, int]]) -> float:
    """Inferred collection cadence: median spacing between consecutive readings (fallback 60s).

    The cadence is operator-owned cron, not stored/configured, so buckets are floored at the data's
    own median spacing (FR-014 / data-model D5) — never finer than the readings.
    """

    if len(rows) < 2:
        return 60.0
    spacings = [
        (t1 - t0) / 1_000_000 for (t0, _), (t1, _) in zip(rows, rows[1:], strict=False) if t1 > t0
    ]
    return statistics.median(spacings) if spacings else 60.0


def _bucketize(
    points: list[tuple[int, float]], *, since_us: int, until_us: int, bucket_us: int
) -> list[Bucket]:
    """Aggregate ``(ts_us, y)`` points into fixed buckets over ``[since_us, until_us)``."""

    n_buckets = max(1, -(-(until_us - since_us) // bucket_us))  # ceil division
    grouped: dict[int, list[float]] = {}
    for ts, y in points:
        idx = (ts - since_us) // bucket_us
        if 0 <= idx < n_buckets:
            grouped.setdefault(int(idx), []).append(y)
    buckets: list[Bucket] = []
    for i in range(n_buckets):
        start_us = since_us + i * bucket_us
        start_iso = datetime.fromtimestamp(start_us / 1_000_000, tz=UTC).isoformat()
        ys = grouped.get(i)
        if ys:
            buckets.append(
                Bucket(
                    start_iso=start_iso, avg=sum(ys) / len(ys), min=min(ys), max=max(ys), n=len(ys)
                )
            )
        else:
            buckets.append(Bucket(start_iso=start_iso, avg=None, min=None, max=None, n=0))
    return buckets


def latest_rate(
    conn: sqlite3.Connection, *, device: str, metric: str, ifname: str
) -> tuple[float, int] | None:
    """Newest per-second rate for one counter series, with the sample's timestamp (feature 095).

    The dashboard's WAN tile needs exactly one number per uplink: the latest measured rate and
    how old it is. Reads the last three samples (two rate intervals of headroom, so a single
    counter reset still yields the fresher interval) and folds them through the same
    reset/gap-aware :func:`_points_for_series` the series query uses. Returns
    ``(rate_per_s, ts_us)`` of the newest valid interval, or ``None`` when the series is
    absent, has a single sample, or every interval was skipped (reset). Read-only.
    """

    rows = conn.execute(
        "SELECT ts_us, value FROM samples "
        "WHERE device = ? AND metric = ? AND ifname = ? AND value IS NOT NULL "
        "ORDER BY ts_us DESC LIMIT 3",
        (device, metric, ifname),
    ).fetchall()
    ordered = [(int(ts), int(value)) for ts, value in reversed(rows)]
    points = _points_for_series(ordered, is_rate=True)
    if not points:
        return None
    ts_us, rate = points[-1]
    return rate, ts_us


def query_series(
    conn: sqlite3.Connection,
    *,
    device: str,
    metric: str,
    since_us: int,
    until_us: int,
    ifname: str | None = None,
    buckets: int = 60,
) -> list[Series]:
    """Return one :class:`Series` per interface (or a single series when ``ifname`` is given).

    Counters are returned as reset/gap-aware per-second rates; gauges as their values. Both are
    downsampled into ~``buckets`` buckets (floored at the inferred cadence). Read-only (SELECT).
    An empty result (no matching readings) yields ``[]`` — the caller renders a clean empty message.
    """

    params: list[object] = [device, metric, since_us, until_us]
    where = "device = ? AND metric = ? AND ts_us >= ? AND ts_us < ? AND value IS NOT NULL"
    if ifname is not None:
        where += " AND ifname = ?"
        params.append(ifname)
    # ifindex is TEXT ('' for device-scalars): CAST orders interfaces numerically ('2' before
    # '10') with the scalar series still first ('' casts to 0).
    sql = (
        f"SELECT ifindex, ifname, kind, ts_us, value FROM samples WHERE {where} "
        "ORDER BY CAST(ifindex AS INTEGER), ts_us"
    )
    by_iface: dict[str, dict[str, object]] = {}
    for ifindex, iface_name, kind, ts_us, value in conn.execute(sql, params):
        entry = by_iface.setdefault(
            str(ifindex), {"ifname": iface_name, "kind": str(kind), "rows": []}
        )
        rows_list = entry["rows"]
        assert isinstance(rows_list, list)
        rows_list.append((int(ts_us), int(value)))

    window_us = max(1, until_us - since_us)
    out: list[Series] = []
    for _ifindex, entry in by_iface.items():
        rows = entry["rows"]
        assert isinstance(rows, list)
        is_rate = str(entry["kind"]) in _COUNTER_KINDS
        floor_s = _floor_seconds(rows)
        bucket_us = max(int(floor_s * 1_000_000), window_us // max(1, buckets))
        bucket_us = max(1, bucket_us)
        points = _points_for_series(rows, is_rate=is_rate)
        divisor = _divisor_for(metric)
        if divisor != 1.0:
            points = [(ts, y / divisor) for ts, y in points]
        iface_label = entry["ifname"]
        out.append(
            Series(
                device=device,
                metric=metric,
                ifname=str(iface_label) if iface_label is not None else None,
                unit=_unit_for(metric, is_rate=is_rate),
                is_rate=is_rate,
                buckets=_bucketize(
                    points, since_us=since_us, until_us=until_us, bucket_us=bucket_us
                ),
            )
        )
    # Device-scalar series (ifindex '') sorts first; interface series follow by ifindex order.
    return out


# --- summaries (feature 090: the model-facing, bucket-free view) -------------------------------


def _contains_pattern(needle: str) -> str:
    """A ``LIKE`` pattern matching ``needle`` as a LITERAL, lower-cased substring.

    ``%``, ``_`` and the escape character itself are escaped, so an operator asking about an
    interface literally named ``100%`` matches that interface rather than every interface.
    """

    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"


def _summary_sort_key(summary: SeriesSummary) -> tuple[int, float, float, str]:
    """Busiest-first: series WITH data before series without, then peak, then avg, then name.

    A measured-idle interface (``samples > 0``, ``peak == 0.0``) therefore outranks one obi has no
    readings for — the useful order when the question is "what's busy?", and one that keeps
    no-data series from masquerading as quiet ones at the bottom of the list.
    """

    return (
        0 if summary.samples else 1,
        -(summary.peak if summary.peak is not None else 0.0),
        -(summary.avg if summary.avg is not None else 0.0),
        summary.ifname or "",
    )


def query_summaries(
    conn: sqlite3.Connection,
    *,
    device: str,
    metrics: Sequence[str],
    since_us: int,
    until_us: int,
    ifname_contains: str | None = None,
) -> list[SeriesSummary]:
    """Summarize each matching series over ``[since_us, until_us)``, busiest first (feature 090).

    One summary per (metric, ifindex). Counters become reset-aware, gap-aware per-second rates and
    gauges keep their values — via :func:`_points_for_series`, the same function behind
    ``obi metrics history`` and the feature-027 watch pass, so this view and the CLI can never
    disagree about what the store says. Read-only (SELECT). No match yields ``[]``.

    ``ifname_contains`` is a case-insensitive literal substring on the interface name; because a
    device scalar has a NULL ``ifname``, an interface filter correctly excludes scalars.
    """

    if not metrics:
        return []
    params: list[object] = [device, *metrics, since_us, until_us]
    placeholders = ", ".join("?" for _ in metrics)
    where = (
        f"device = ? AND metric IN ({placeholders}) AND ts_us >= ? AND ts_us < ? "
        "AND value IS NOT NULL"
    )
    if ifname_contains is not None:
        where += " AND LOWER(ifname) LIKE ? ESCAPE '\\'"
        params.append(_contains_pattern(ifname_contains))
    sql = (
        f"SELECT metric, ifindex, ifname, kind, ts_us, value FROM samples WHERE {where} "
        "ORDER BY metric, CAST(ifindex AS INTEGER), ts_us"
    )
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for metric, ifindex, iface_name, kind, ts_us, value in conn.execute(sql, params):
        entry = grouped.setdefault(
            (str(metric), str(ifindex)), {"ifname": iface_name, "kind": str(kind), "rows": []}
        )
        rows_list = entry["rows"]
        assert isinstance(rows_list, list)
        rows_list.append((int(ts_us), int(value)))

    out: list[SeriesSummary] = []
    for (metric_name, _ifindex), entry in grouped.items():
        rows = entry["rows"]
        assert isinstance(rows, list)
        is_rate = str(entry["kind"]) in _COUNTER_KINDS
        divisor = _divisor_for(metric_name)
        points = [y for _ts, y in _points_for_series(rows, is_rate=is_rate)]
        if divisor != 1.0:
            points = [y / divisor for y in points]
        iface_label = entry["ifname"]
        out.append(
            SeriesSummary(
                device=device,
                metric=metric_name,
                ifname=str(iface_label) if iface_label is not None else None,
                unit=_unit_for(metric_name, is_rate=is_rate),
                is_rate=is_rate,
                avg=(sum(points) / len(points)) if points else None,
                peak=max(points) if points else None,
                low=min(points) if points else None,
                samples=len(points),
                first_us=rows[0][0] if rows else None,
                last_us=rows[-1][0] if rows else None,
            )
        )
    out.sort(key=_summary_sort_key)
    return out


#: A "loose index scan": walk the DISTINCT values of ``device`` by seeking to the next one, rather
#: than scanning every row to group them. SQLite has no skip-scan, and a plain ``GROUP BY device``
#: over a multi-gigabyte store takes SECONDS (measured: ~6 s on the live 4.7 GB store; ~35 s once
#: it reached 6.9 GB / 27.7M rows) — far too slow for a message that exists only to be helpful.
#: This costs one index seek per device: ~10 ms on that same store, and flat as the store grows.
#: Feature 041's ``/api/metrics.json`` picker rides this too — see :func:`devices`.
_DISTINCT_DEVICES = """
WITH RECURSIVE walk(name) AS (
    SELECT MIN(device) FROM samples
    UNION ALL
    SELECT (SELECT MIN(device) FROM samples WHERE device > walk.name)
    FROM walk WHERE walk.name IS NOT NULL
)
SELECT name FROM walk WHERE name IS NOT NULL ORDER BY name
"""

#: How far back the interface-name lookup looks from a device's most recent reading. One day is
#: plenty to enumerate a device's ports and keeps the scan to a few hundred indexed rows.
_IFNAME_LOOKBACK_US = 24 * 60 * 60 * 1_000_000


def devices(conn: sqlite3.Connection) -> list[str]:
    """Every device identity with a stored reading, sorted — the "which devices DO have history"
    candidate list behind the feature-090 miss message, and the device picker MET fills from
    ``/api/metrics.json`` (feature 041). Tolerates a store with no schema."""

    try:
        rows = conn.execute(_DISTINCT_DEVICES).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(row[0]) for row in rows]


def interfaces_for(
    conn: sqlite3.Connection, device: str, *, metrics: Sequence[str] = ()
) -> list[str]:
    """Interface names recently stored for ``device``, in numeric ifindex order — the candidate
    list behind the "no such interface" message. Device scalars (NULL ``ifname``) are not
    interfaces.

    Bounded to the day preceding the device's most recent reading for ``metrics``: the names of a
    router's ports are the same in every cycle, so scanning ninety days of them to list nineteen
    strings is pure waste (and, on the live store, seconds of it). ``metrics`` empty ⇒ every metric.
    """

    metric_filter = ""
    metric_params: list[object] = []
    if metrics:
        metric_filter = f" AND metric IN ({', '.join('?' for _ in metrics)})"
        metric_params = list(metrics)
    try:
        row = conn.execute(
            f"SELECT MAX(ts_us) FROM samples WHERE device = ?{metric_filter}",
            (device, *metric_params),
        ).fetchone()
        if row is None or row[0] is None:
            return []
        floor_us = int(row[0]) - _IFNAME_LOOKBACK_US
        rows = conn.execute(
            "SELECT ifname, MIN(CAST(ifindex AS INTEGER)) AS ix FROM samples "
            f"WHERE device = ?{metric_filter} AND ts_us >= ? AND ifname IS NOT NULL "
            "GROUP BY ifname ORDER BY ix",
            (device, *metric_params, floor_us),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(row[0]) for row in rows]


def oldest_us(conn: sqlite3.Connection) -> int | None:
    """Epoch-µs of the oldest reading in the store; ``None`` when empty.

    Deliberately NOT :func:`coverage` — this is a ``MIN()`` over an indexed column (an index seek),
    where coverage's ``COUNT(*)`` scans the whole table. It turns "that window is empty" into a
    diagnosis (collection hadn't started / retention rolled over) for the price of a seek.
    """

    try:
        row = conn.execute("SELECT MIN(ts_us) FROM samples").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row is not None and row[0] is not None else None


# --- latest readings per series (feature 096: device health) -----------------------------------


def recent_values(
    conn: sqlite3.Connection, *, device: str, metric: str, ifindex: str, n: int
) -> list[int]:
    """The newest ``n`` raw values of one series, newest first (feature 096: the watch
    pass's sustained-condition gate). Index-assisted (device, metric, ts_us); read-only."""

    rows = conn.execute(
        "SELECT value FROM samples WHERE device = ? AND metric = ? AND ifindex = ? "
        "ORDER BY ts_us DESC LIMIT ?",
        (device, metric, ifindex, n),
    ).fetchall()
    return [int(r[0]) for r in rows]


def display_unit(metric: str) -> str:
    """Public: the display unit for a stored (non-rate) metric — the 096 surfaces' accessor."""

    return _unit_for(metric, is_rate=False)


def display_value(metric: str, raw: int) -> float:
    """Public: a stored raw value scaled to agree with :func:`display_unit` (096 surfaces)."""

    return raw / _divisor_for(metric)


@dataclass(frozen=True)
class LatestSample:
    """The newest stored reading of one series — (device, metric, ifindex) — within a window."""

    device: str
    profile: str
    metric: str
    ifindex: str
    ifname: str | None
    kind: str
    value: int
    ts_us: int
    ts_iso: str
    address: str | None = None


def latest_samples(
    conn: sqlite3.Connection,
    *,
    prefixes: Sequence[str],
    since_us: int,
    device: str | None = None,
) -> list[LatestSample]:
    """Newest reading per (device, metric, ifindex) series whose metric starts with one of
    ``prefixes``, considering only rows at/after ``since_us`` (feature 096). Read-only.

    ONE index-range scan over ``idx_samples_ts`` (the recent window), never a full-table pass —
    the feature-090 lesson about the multi-GB live store. A series whose newest row predates the
    window simply does not appear; per-device staleness stays :func:`device_freshness`'s job.
    """

    if not prefixes:
        return []
    conditions = ["ts_us >= ?"]
    params: list[object] = [since_us]
    conditions.append("(" + " OR ".join("metric LIKE ?" for _ in prefixes) + ")")
    params.extend(f"{p}%" for p in prefixes)
    if device is not None:
        conditions.append("device = ?")
        params.append(device)
    sql = (
        "SELECT device, profile, metric, ifindex, ifname, kind, value, ts_us, ts_iso, address "
        f"FROM samples WHERE {' AND '.join(conditions)}"
    )
    newest: dict[tuple[str, str, str], LatestSample] = {}
    for row in conn.execute(sql, params):
        key = (str(row[0]), str(row[2]), str(row[3]))
        current = newest.get(key)
        if current is None or int(row[7]) > current.ts_us:
            newest[key] = LatestSample(
                device=str(row[0]),
                profile=str(row[1]),
                metric=str(row[2]),
                ifindex=str(row[3]),
                ifname=str(row[4]) if row[4] is not None else None,
                kind=str(row[5]),
                value=int(row[6]),
                ts_us=int(row[7]),
                ts_iso=str(row[8]),
                address=str(row[9]) if row[9] is not None else None,
            )
    return [newest[key] for key in sorted(newest)]


# --- counter-increase detection (feature 027: watch-on-metrics) --------------------------------


@dataclass
class CounterIncrease:
    """How much a monotonic counter climbed over a window, for one device+interface+metric.

    ``increase`` is the **reset-aware** sum of positive increments between consecutive readings — a
    decrease (reboot/wrap) contributes 0, never a negative — so a reset in the window cannot
    fabricate or cancel a detection (mirrors :func:`_points_for_series`).
    """

    device: str
    address: str
    ifname: str
    metric: str
    increase: int
    start_value: int
    end_value: int
    first_ts_us: int
    last_ts_us: int
    readings: int


def counter_increases(
    conn: sqlite3.Connection,
    *,
    metrics: list[str],
    since_us: int,
    until_us: int,
    min_increase: int,
) -> list[CounterIncrease]:
    """Return each (device, interface, metric) whose counter increased by more than ``min_increase``
    over ``[since_us, until_us)`` (feature 027). Read-only (SELECT only).

    Groups by (device, metric, ifindex); a group with fewer than two readings has no baseline and
    is skipped. The increase is the reset-aware sum of positive consecutive increments;
    ``min_increase`` of 0 means any positive increase qualifies.
    """

    if not metrics:
        return []
    placeholders = ",".join("?" for _ in metrics)
    sql = (
        f"SELECT device, address, ifname, ifindex, metric, ts_us, value FROM samples "
        f"WHERE metric IN ({placeholders}) AND ts_us >= ? AND ts_us < ? "
        "ORDER BY device, metric, ifindex, ts_us"
    )
    params: list[object] = [*metrics, since_us, until_us]

    groups: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
    meta: dict[tuple[str, str, str], tuple[str, str, str, str]] = {}
    for device, address, ifname, ifindex, metric, ts_us, value in conn.execute(sql, params):
        key = (str(device), str(metric), str(ifindex))
        groups.setdefault(key, []).append((int(ts_us), int(value)))
        meta[key] = (str(device), str(address), str(ifname or ""), str(metric))

    out: list[CounterIncrease] = []
    for key, rows in groups.items():
        if len(rows) < 2:  # no baseline to compare against
            continue
        increase = sum(max(0, v1 - v0) for (_, v0), (_, v1) in zip(rows, rows[1:], strict=False))
        if increase <= min_increase:
            continue
        device, address, ifname, metric = meta[key]
        out.append(
            CounterIncrease(
                device=device,
                address=address,
                ifname=ifname,
                metric=metric,
                increase=increase,
                start_value=rows[0][1],
                end_value=rows[-1][1],
                first_ts_us=rows[0][0],
                last_ts_us=rows[-1][0],
                readings=len(rows),
            )
        )
    out.sort(key=lambda c: (c.device, c.ifname, c.metric))
    return out