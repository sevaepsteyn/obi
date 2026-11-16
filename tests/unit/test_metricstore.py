"""Unit (feature 021): the SNMP time-series store — insert/idempotency, rate math, bucketing,
retention prune, and coverage.

Covers the tricky query-time logic: counter → per-second rate, a reset (decrease) dropping only its
interval, a collection gap spanning with the real elapsed time (no spike), exact 64-bit precision,
gauge values, the all-interfaces-vs-single default, empty results, retention, and coverage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import metricstore
from obi.core.metricstore import StoredSample

_BASE = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)


def _us(offset_s: float) -> int:
    return int((_BASE.timestamp() + offset_s) * 1_000_000)


def _sample(
    metric: str,
    ts_off: float,
    value: int,
    *,
    device: str = "core-gw-1",
    ifindex: str = "1",
    ifname: str | None = "ether1",
    kind: str = "counter64",
    profile: str = "interfaces",
    address: str = "172.18.75.1",
) -> StoredSample:
    t = _us(ts_off)
    return StoredSample(
        ts_us=t,
        ts_iso=datetime.fromtimestamp(t / 1_000_000, tz=UTC).isoformat(),
        device=device,
        address=address,
        profile=profile,
        metric=metric,
        ifindex=ifindex,
        ifname=ifname,
        kind=kind,
        value=value,
    )


def _store(tmp_path: Path, rows: list[StoredSample]):
    conn = metricstore.connect(tmp_path / "metrics.db")
    metricstore.insert_samples(conn, rows)
    conn.commit()
    return conn


def _nonempty(series: metricstore.Series) -> list[metricstore.Bucket]:
    return [b for b in series.buckets if b.n > 0]


# --- schema / insert / idempotency (T005) -----------------------------------------------------


def test_schema_applies_and_roundtrips_exact_value(tmp_path: Path) -> None:
    big = 2**53 + 3  # beyond float precision; INTEGER storage must return it exactly
    conn = _store(tmp_path, [_sample("ifHCInOctets", 0, big)])
    row = conn.execute("SELECT value FROM samples").fetchone()
    assert row[0] == big


def test_insert_is_idempotent(tmp_path: Path) -> None:
    conn = metricstore.connect(tmp_path / "m.db")
    row = _sample("ifHCInOctets", 0, 1000)
    assert metricstore.insert_samples(conn, [row]) == 1
    assert metricstore.insert_samples(conn, [row]) == 0  # same (ts,device,metric,ifindex) ignored
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_scalar_and_interface_rows_coexist(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("cpuLoad", 0, 40, ifindex="", ifname=None, kind="gauge", profile="system"),
            _sample("ifHCInOctets", 0, 1000, ifindex="1", ifname="ether1"),
        ],
    )
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2


# --- counter rate math (T011) -----------------------------------------------------------------


def _q(conn, metric, **kw):
    return metricstore.query_series(
        conn, device="core-gw-1", metric=metric, since_us=_us(-60), until_us=_us(3600), **kw
    )


def test_counter_rate_is_delta_over_elapsed(tmp_path: Path) -> None:
    # +600_000 bytes over 300 s → 2000 bytes/s.
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 1_000_000),
            _sample("ifHCInOctets", 300, 1_600_000),
        ],
    )
    (series,) = _q(conn, "ifHCInOctets")
    assert series.is_rate is True
    assert series.unit == "bytes/s"
    buckets = _nonempty(series)
    assert len(buckets) == 1
    assert buckets[0].avg == 2000.0


def test_counter_reset_drops_only_its_interval(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 1_000),
            _sample("ifHCInOctets", 300, 5_000),  # +4000/300 = 13.33/s
            _sample("ifHCInOctets", 600, 2_000),  # decrease → reset → dropped
            _sample("ifHCInOctets", 900, 8_000),  # +6000/300 = 20/s
        ],
    )
    (series,) = _q(conn, "ifHCInOctets")
    rates = sorted(b.avg for b in _nonempty(series))
    assert len(rates) == 2  # the reset interval is excluded, not rendered as a negative spike
    assert all(r is not None and r > 0 for r in rates)
    assert max(rates) == 20.0


def test_gap_uses_actual_elapsed_time(tmp_path: Path) -> None:
    # A missed cycle: 900 s between readings, not the usual 300. Rate must use 900, not spike.
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 0),
            _sample("ifHCInOctets", 900, 600_000),
        ],
    )
    (series,) = _q(conn, "ifHCInOctets")
    (bucket,) = _nonempty(series)
    assert bucket.avg is not None
    assert round(bucket.avg, 2) == round(600_000 / 900, 2)  # ~666.67, NOT 2000


def test_first_reading_is_baseline_only(tmp_path: Path) -> None:
    conn = _store(tmp_path, [_sample("ifHCInOctets", 0, 1_000_000)])
    (series,) = _q(conn, "ifHCInOctets")
    assert _nonempty(series) == []  # one reading → no rate yet


def test_rate_precision_beyond_float(tmp_path: Path) -> None:
    # Δ=3 at magnitude 2**53 where float64 ULP is 2 — a REAL column would corrupt the delta.
    v0 = 2**53
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, v0),
            _sample("ifHCInOctets", 1, v0 + 3),  # +3 over 1 s → exactly 3.0/s
        ],
    )
    (series,) = _q(conn, "ifHCInOctets")
    (bucket,) = _nonempty(series)
    assert bucket.avg == 3.0


# --- gauge / bucketing / all-interfaces / empty (T012) ----------------------------------------


def test_gauge_series_returns_values(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("cpuLoad", 0, 40, ifindex="", ifname=None, kind="gauge", profile="system"),
            _sample("cpuLoad", 300, 60, ifindex="", ifname=None, kind="gauge", profile="system"),
        ],
    )
    (series,) = _q(conn, "cpuLoad")
    assert series.is_rate is False
    assert series.unit == "%"
    vals = sorted(b.avg for b in _nonempty(series))
    assert vals == [40.0, 60.0]


def test_all_interfaces_default_one_series_each(tmp_path: Path) -> None:
    rows = []
    for idx, name in (("1", "ether1"), ("2", "ether2")):
        rows += [
            _sample("ifHCInOctets", 0, 1000, ifindex=idx, ifname=name),
            _sample("ifHCInOctets", 300, 4000, ifindex=idx, ifname=name),
        ]
    conn = _store(tmp_path, rows)
    series = _q(conn, "ifHCInOctets")
    assert {s.ifname for s in series} == {"ether1", "ether2"}


def test_interface_filter_narrows_to_one(tmp_path: Path) -> None:
    rows = []
    for idx, name in (("1", "ether1"), ("2", "ether2")):
        rows += [
            _sample("ifHCInOctets", 0, 1000, ifindex=idx, ifname=name),
            _sample("ifHCInOctets", 300, 4000, ifindex=idx, ifname=name),
        ]
    conn = _store(tmp_path, rows)
    series = _q(conn, "ifHCInOctets", ifname="ether2")
    assert len(series) == 1
    assert series[0].ifname == "ether2"


def test_empty_result_is_empty_list(tmp_path: Path) -> None:
    conn = metricstore.connect(tmp_path / "m.db")
    assert _q(conn, "ifHCInOctets") == []


def test_bucketing_is_bounded_and_marks_gaps(tmp_path: Path) -> None:
    # 24 gauge readings 5 min apart; a small bucket target must still bound the series, and a
    # missing stretch must surface as empty (n==0) buckets.
    rows = [
        _sample("cpuLoad", 300 * i, 50, ifindex="", ifname=None, kind="gauge", profile="system")
        for i in range(6)
    ]
    conn = _store(tmp_path, rows)
    series = metricstore.query_series(
        conn,
        device="core-gw-1",
        metric="cpuLoad",
        since_us=_us(0),
        until_us=_us(7200),
        buckets=6,
    )
    (s,) = series
    assert len(s.buckets) <= 12  # bounded, not one-per-reading
    assert any(b.n == 0 for b in s.buckets)  # the empty tail is a gap


# --- prune + coverage (T018) ------------------------------------------------------------------


def test_prune_removes_old_keeps_new(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 1000),
            _sample("ifHCInOctets", 3600, 2000),
        ],
    )
    removed = metricstore.prune(conn, older_than_us=_us(1800))
    conn.commit()
    assert removed == 1
    remaining = conn.execute("SELECT ts_us FROM samples").fetchall()
    assert len(remaining) == 1
    assert remaining[0][0] == _us(3600)


def test_coverage_reports_totals_and_devices(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 1000, device="core-gw-1"),
            _sample("ifHCInOctets", 300, 2000, device="core-gw-1"),
            _sample(
                "cpuLoad",
                0,
                50,
                device="chestnut-gw",
                ifindex="",
                ifname=None,
                kind="gauge",
                profile="system",
            ),
        ],
    )
    cov = metricstore.coverage(conn)
    assert cov.total_readings == 3
    assert cov.oldest_us == _us(0)
    assert cov.newest_us == _us(300)
    by_device = {d.device: d.readings for d in cov.devices}
    assert by_device == {"core-gw-1": 2, "chestnut-gw": 1}


def test_coverage_empty_store(tmp_path: Path) -> None:
    conn = metricstore.connect(tmp_path / "m.db")
    cov = metricstore.coverage(conn)
    assert cov.total_readings == 0
    assert cov.devices == []


# --- interface ordering + gauge units (metrics-pipeline fix, 2026-07-11) ------------------------


def test_interfaces_order_numerically_not_lexicographically(tmp_path: Path) -> None:
    """ifindex is TEXT: without the CAST, '10' sorts before '2' and a many-port router's
    series come back in a jumbled order."""

    rows = []
    for ifindex, ifname in [("10", "ether10"), ("2", "ether2")]:
        rows += [
            _sample("ifHCInOctets", 0, 0, ifindex=ifindex, ifname=ifname),
            _sample("ifHCInOctets", 300, 300_000, ifindex=ifindex, ifname=ifname),
        ]
    conn = _store(tmp_path, rows)
    series = _q(conn, "ifHCInOctets")
    assert [s.ifname for s in series] == ["ether2", "ether10"]


def test_known_gauges_carry_display_units(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("cpuLoad", 0, 7, ifindex="", ifname=None, kind="gauge", profile="system"),
            _sample("memUsedKb", 0, 1024, ifindex="", ifname=None, kind="gauge",
                    profile="system"),
            _sample("cpuTemperature", 0, 47, ifindex="", ifname=None, kind="integer",
                    profile="system"),
        ],
    )
    units = {m: _q(conn, m)[0].unit for m in ("cpuLoad", "memUsedKb", "cpuTemperature")}
    assert units == {"cpuLoad": "%", "memUsedKb": "kB", "cpuTemperature": "°C"}


def test_temperature_scales_deci_degrees_to_celsius(tmp_path: Path) -> None:
    """MIKROTIK-MIB reports mtxrHl*Temperature in tenths of a degree: a stored 430 is 43 °C, not
    430. Display divides by 10 so the value matches the °C unit; other gauges are untouched."""

    conn = _store(
        tmp_path,
        [
            _sample("cpuTemperature", 0, 430, ifindex="", ifname=None, kind="integer",
                    profile="system"),
            _sample("boardTemperature", 0, 355, ifindex="", ifname=None, kind="integer",
                    profile="system"),
            _sample("cpuLoad", 0, 40, ifindex="", ifname=None, kind="gauge", profile="system"),
        ],
    )
    assert _nonempty(_q(conn, "cpuTemperature")[0])[0].avg == 43.0
    assert _nonempty(_q(conn, "boardTemperature")[0])[0].avg == 35.5
    assert _nonempty(_q(conn, "cpuLoad")[0])[0].avg == 40.0  # non-temperature gauge unchanged


# --- 096 health/optics/linux display units + divisor precedence (T002) ------------------------


def test_health_units_scale_per_sensor_kind(tmp_path: Path) -> None:
    """Health sensors ride the store's raw-value convention: temps are deci-°C (normalized at
    ingest), dV/dA/dW stay raw with a ÷10 display divisor, RPM and status are 1:1."""

    conn = _store(
        tmp_path,
        [
            _sample("health:cpu-temperature", 0, 350, ifindex="17", ifname="cpu-temperature",
                    kind="gauge", profile="health"),
            _sample("health:2pin-voltage", 0, 545, ifindex="7201", ifname="2pin-voltage",
                    kind="gauge", profile="health"),
            _sample("health:psu-current", 0, 12, ifindex="7301", ifname="psu-current",
                    kind="gauge", profile="health"),
            _sample("health:poe-out-consumption", 0, 175, ifindex="16",
                    ifname="poe-out-consumption", kind="gauge", profile="health"),
            _sample("health:fan1-speed", 0, 3870, ifindex="7001", ifname="fan1-speed",
                    kind="gauge", profile="health"),
            _sample("health:psu1-state", 0, 0, ifindex="7401", ifname="psu1-state",
                    kind="gauge", profile="health"),
        ],
    )

    def one(metric: str) -> tuple[float, str]:
        series = _q(conn, metric)[0]
        return _nonempty(series)[0].avg, series.unit

    assert one("health:cpu-temperature") == (35.0, "°C")
    assert one("health:2pin-voltage") == (54.5, "V")
    assert one("health:psu-current") == (1.2, "A")
    assert one("health:poe-out-consumption") == (17.5, "W")
    assert one("health:fan1-speed") == (3870.0, "rpm")
    assert one("health:psu1-state") == (0.0, "")


def test_optics_suffix_branches_beat_generic_words(tmp_path: Path) -> None:
    """The I1 precedence rule: exact suffixes (mdbm/mv/ma) match BEFORE the generic voltage/power
    words they contain — otherwise a 3225 mV supply would render as 322.5 "V" and an rx power as
    watts. Signed mdBm divides by 1000 into dBm."""

    conn = _store(
        tmp_path,
        [
            _sample("optics:supplyVoltageMv", 0, 3225, ifindex="2", ifname="sfp-sfpplus1",
                    kind="gauge", profile="optics"),
            _sample("optics:rxPowerMdbm", 0, -1907, ifindex="2", ifname="sfp-sfpplus1",
                    kind="gauge", profile="optics"),
            _sample("optics:txPowerMdbm", 0, -2274, ifindex="2", ifname="sfp-sfpplus1",
                    kind="gauge", profile="optics"),
            _sample("optics:txBiasMa", 0, 6, ifindex="2", ifname="sfp-sfpplus1",
                    kind="gauge", profile="optics"),
            _sample("optics:temperature", 0, 340, ifindex="2", ifname="sfp-sfpplus1",
                    kind="gauge", profile="optics"),
        ],
    )

    def one(metric: str) -> tuple[float, str]:
        series = _q(conn, metric)[0]
        return _nonempty(series)[0].avg, series.unit

    assert one("optics:supplyVoltageMv") == (3225.0, "mV")
    assert one("optics:rxPowerMdbm") == (-1.907, "dBm")
    assert one("optics:txPowerMdbm") == (-2.274, "dBm")
    assert one("optics:txBiasMa") == (6.0, "mA")
    assert one("optics:temperature") == (34.0, "°C")


def test_linux_percent_and_kb_units(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("linux:cpuUtilPercent", 0, 43, ifindex="", ifname=None, kind="gauge",
                    profile="linux-health", device="core-vms-1"),
            _sample("linux:diskUsedPercent", 0, 412, ifindex="/", ifname="/", kind="gauge",
                    profile="linux-health", device="core-vms-1"),
            _sample("linux:memTotalKb", 0, 8_192_000, ifindex="", ifname=None, kind="gauge",
                    profile="linux-health", device="core-vms-1"),
            _sample("linux:temperatureC", 0, 425, ifindex="hwmon0/temp1", ifname="coretemp",
                    kind="gauge", profile="linux-health", device="core-vms-1"),
        ],
    )

    def one(metric: str) -> tuple[float, str]:
        series = metricstore.query_series(
            conn, device="core-vms-1", metric=metric, since_us=_us(-60), until_us=_us(3600)
        )[0]
        return _nonempty(series)[0].avg, series.unit

    assert one("linux:cpuUtilPercent") == (4.3, "%")
    assert one("linux:diskUsedPercent") == (41.2, "%")
    assert one("linux:memTotalKb") == (8_192_000.0, "kB")
    assert one("linux:temperatureC") == (42.5, "°C")
