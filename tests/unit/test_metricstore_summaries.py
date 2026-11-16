"""Unit (feature 090, T002): ``metricstore.query_summaries`` — the bounded, model-facing summary.

Where ``query_series`` (021) returns ~60 buckets per series for charts, ``query_summaries``
collapses each series to avg/peak/low + a sample COUNT so a 90-day answer is the same size as a
1-hour one. The tricky parts are inherited from ``_points_for_series`` (reset- and gap-aware rate
math) and asserted here at the summary level, plus the two facts the renderer depends on: the
busiest-first ordering, and ``samples == 0`` being distinct from ``avg == 0.0``.
"""

from __future__ import annotations

import sqlite3
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
    device: str = "tamarack-internet-gw",
    ifindex: str = "1",
    ifname: str | None = "sfp-sfpplus5-sycamore",
    kind: str = "counter64",
    profile: str = "interfaces",
    address: str = "172.18.81.1",
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


def _store(tmp_path: Path, rows: list[StoredSample]) -> sqlite3.Connection:
    conn = metricstore.connect(tmp_path / "metrics.db")
    metricstore.insert_samples(conn, rows)
    conn.commit()
    return conn


def _summaries(
    conn: sqlite3.Connection,
    *,
    metrics: tuple[str, ...] = ("ifHCInOctets",),
    device: str = "tamarack-internet-gw",
    ifname_contains: str | None = None,
    window_s: float = 3600.0,
) -> list[metricstore.SeriesSummary]:
    return metricstore.query_summaries(
        conn,
        device=device,
        metrics=metrics,
        since_us=_us(-1),
        until_us=_us(window_s),
        ifname_contains=ifname_contains,
    )


# --- counter → rate ---------------------------------------------------------------------------


def test_counter_becomes_a_per_second_rate(tmp_path: Path) -> None:
    # +1_000_000 bytes over 10 s, then +2_000_000 over 10 s → 100 kB/s then 200 kB/s.
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 0),
            _sample("ifHCInOctets", 10, 1_000_000),
            _sample("ifHCInOctets", 20, 3_000_000),
        ],
    )
    (summary,) = _summaries(conn)
    assert summary.is_rate is True
    assert summary.unit == "bytes/s"
    assert summary.samples == 2  # n readings ⇒ n-1 intervals
    assert summary.low == 100_000.0
    assert summary.peak == 200_000.0
    assert summary.avg == 150_000.0
    assert summary.low <= summary.avg <= summary.peak


def test_counter_reset_drops_only_its_interval(tmp_path: Path) -> None:
    # A reboot resets the counter; that interval must contribute nothing — never a negative rate.
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 5_000_000),
            _sample("ifHCInOctets", 10, 6_000_000),  # +100 kB/s
            _sample("ifHCInOctets", 20, 10_000),  # reset — dropped
            _sample("ifHCInOctets", 30, 1_010_000),  # +100 kB/s
        ],
    )
    (summary,) = _summaries(conn)
    assert summary.samples == 2  # three intervals, one dropped
    assert summary.peak == 100_000.0
    assert summary.low == 100_000.0


def test_collection_gap_is_not_averaged_in_as_zero(tmp_path: Path) -> None:
    # A 10-minute gap with real traffic spans at the true elapsed time — it neither spikes nor
    # contributes a zero sample that would drag the average down.
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 0),
            _sample("ifHCInOctets", 10, 1_000_000),  # 100 kB/s
            _sample("ifHCInOctets", 610, 61_000_000),  # 600 s gap, 100 kB/s throughout
        ],
    )
    (summary,) = _summaries(conn)
    assert summary.samples == 2
    assert summary.avg == 100_000.0
    assert summary.peak == 100_000.0


def test_single_reading_yields_no_rate_but_is_reported(tmp_path: Path) -> None:
    # One odometer value is not a speed: samples == 0, values None — the FR-011 discriminator.
    conn = _store(tmp_path, [_sample("ifHCInOctets", 0, 12_345)])
    (summary,) = _summaries(conn)
    assert summary.samples == 0
    assert summary.avg is None and summary.peak is None and summary.low is None
    assert summary.first_us is not None  # we still know when the one reading landed


def test_zero_traffic_is_distinct_from_no_data(tmp_path: Path) -> None:
    # A genuinely idle interface HAS data: samples > 0 and avg == 0.0. Never conflate the two.
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 7_000),
            _sample("ifHCInOctets", 10, 7_000),
            _sample("ifHCInOctets", 20, 7_000),
        ],
    )
    (summary,) = _summaries(conn)
    assert summary.samples == 2
    assert summary.avg == 0.0 and summary.peak == 0.0


# --- gauges -----------------------------------------------------------------------------------


def test_gauge_reports_values_not_rates(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("cpuLoad", 0, 10, ifindex="", ifname=None, kind="gauge", profile="system"),
            _sample("cpuLoad", 10, 30, ifindex="", ifname=None, kind="gauge", profile="system"),
        ],
    )
    (summary,) = _summaries(conn, metrics=("cpuLoad",))
    assert summary.is_rate is False
    assert summary.ifname is None
    assert summary.unit == "%"
    assert summary.samples == 2  # gauges: one point per reading, no differencing
    assert (summary.low, summary.avg, summary.peak) == (10.0, 20.0, 30.0)


def test_temperature_gauge_is_scaled_out_of_deci_celsius(tmp_path: Path) -> None:
    # MIKROTIK-MIB reports tenths of a degree; the summary must agree with its own "°C" unit.
    conn = _store(
        tmp_path,
        [
            _sample(
                "cpuTemperature", 0, 430, ifindex="", ifname=None, kind="gauge", profile="system"
            ),
        ],
    )
    (summary,) = _summaries(conn, metrics=("cpuTemperature",))
    assert summary.unit == "°C"
    assert summary.avg == 43.0


# --- ordering, filtering, scoping -------------------------------------------------------------


def test_summaries_are_ordered_busiest_first(tmp_path: Path) -> None:
    rows: list[StoredSample] = []
    lanes = (("1", "busy", 10_000_000), ("2", "quiet", 1_000), ("3", "idle", 0))
    for ifindex, ifname, step in lanes:
        rows += [
            _sample("ifHCInOctets", 0, 0, ifindex=ifindex, ifname=ifname),
            _sample("ifHCInOctets", 10, step, ifindex=ifindex, ifname=ifname),
        ]
    # An interface with a single reading (no derivable rate) must sort BELOW a measured idle one.
    rows.append(_sample("ifHCInOctets", 0, 5, ifindex="4", ifname="nodata"))
    conn = _store(tmp_path, rows)
    assert [s.ifname for s in _summaries(conn)] == ["busy", "quiet", "idle", "nodata"]


def test_ifname_filter_is_a_case_insensitive_substring(tmp_path: Path) -> None:
    rows = [
        _sample("ifHCInOctets", 0, 0, ifindex="1", ifname="sfp-sfpplus5-sycamore"),
        _sample("ifHCInOctets", 10, 100, ifindex="1", ifname="sfp-sfpplus5-sycamore"),
        _sample("ifHCInOctets", 0, 0, ifindex="2", ifname="ether1-manage"),
        _sample("ifHCInOctets", 10, 100, ifindex="2", ifname="ether1-manage"),
    ]
    conn = _store(tmp_path, rows)
    assert [s.ifname for s in _summaries(conn, ifname_contains="Sycamore")] == [
        "sfp-sfpplus5-sycamore"
    ]
    assert _summaries(conn, ifname_contains="nope") == []


def test_ifname_filter_wildcards_are_literal(tmp_path: Path) -> None:
    # "%" must match a literal percent sign, not every interface (SQL LIKE injection).
    rows = [
        _sample("ifHCInOctets", 0, 0, ifindex="1", ifname="ether1"),
        _sample("ifHCInOctets", 10, 100, ifindex="1", ifname="ether1"),
    ]
    conn = _store(tmp_path, rows)
    assert _summaries(conn, ifname_contains="%") == []


def test_ifname_filter_excludes_device_scalars(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("cpuLoad", 0, 10, ifindex="", ifname=None, kind="gauge", profile="system"),
            _sample("cpuLoad", 10, 20, ifindex="", ifname=None, kind="gauge", profile="system"),
        ],
    )
    assert _summaries(conn, metrics=("cpuLoad",), ifname_contains="ether") == []


def test_query_is_scoped_to_one_device_and_the_window(tmp_path: Path) -> None:
    rows = [
        _sample("ifHCInOctets", 0, 0),
        _sample("ifHCInOctets", 10, 1_000),
        _sample("ifHCInOctets", 0, 0, device="core-gw-1"),
        _sample("ifHCInOctets", 10, 9_000_000, device="core-gw-1"),
    ]
    conn = _store(tmp_path, rows)
    (summary,) = _summaries(conn)
    assert summary.device == "tamarack-internet-gw"
    assert summary.peak == 100.0
    # A window that predates every reading matches nothing at all.
    assert (
        metricstore.query_summaries(
            conn,
            device="tamarack-internet-gw",
            metrics=("ifHCInOctets",),
            since_us=_us(-7200),
            until_us=_us(-3600),
        )
        == []
    )


def test_several_metrics_in_one_call_yield_one_summary_each(tmp_path: Path) -> None:
    rows = [
        _sample("ifHCInOctets", 0, 0),
        _sample("ifHCInOctets", 10, 1_000),
        _sample("ifHCOutOctets", 0, 0),
        _sample("ifHCOutOctets", 10, 500),
    ]
    conn = _store(tmp_path, rows)
    summaries = _summaries(conn, metrics=("ifHCInOctets", "ifHCOutOctets"))
    assert {(s.metric, s.peak) for s in summaries} == {
        ("ifHCInOctets", 100.0),
        ("ifHCOutOctets", 50.0),
    }


def test_no_metrics_requested_returns_nothing(tmp_path: Path) -> None:
    conn = _store(tmp_path, [_sample("ifHCInOctets", 0, 0)])
    assert _summaries(conn, metrics=()) == []


# --- candidate lists (T004) -------------------------------------------------------------------


def test_devices_lists_every_device_with_history(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 0),
            _sample("ifHCInOctets", 0, 0, device="core-gw-1"),
        ],
    )
    assert metricstore.devices(conn) == ["core-gw-1", "tamarack-internet-gw"]


def test_interfaces_for_lists_names_in_ifindex_order_without_scalars(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _sample("ifHCInOctets", 0, 0, ifindex="10", ifname="sfp-sfpplus10"),
            _sample("ifHCInOctets", 0, 0, ifindex="2", ifname="ether1-manage"),
            _sample("cpuLoad", 0, 5, ifindex="", ifname=None, kind="gauge", profile="system"),
        ],
    )
    # numeric ifindex order ('2' before '10'), and the device scalar is not an interface
    assert metricstore.interfaces_for(conn, "tamarack-internet-gw") == [
        "ether1-manage",
        "sfp-sfpplus10",
    ]


def test_oldest_us_reports_the_first_reading_in_the_store(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [_sample("ifHCInOctets", 500, 0), _sample("ifHCInOctets", 100, 0, ifindex="2")],
    )
    assert metricstore.oldest_us(conn) == _us(100)


def test_candidate_lists_tolerate_an_empty_store(tmp_path: Path) -> None:
    conn = _store(tmp_path, [])
    assert metricstore.devices(conn) == []
    assert metricstore.interfaces_for(conn, "tamarack-internet-gw") == []
    assert metricstore.oldest_us(conn) is None
