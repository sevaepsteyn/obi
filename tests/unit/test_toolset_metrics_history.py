"""Unit (feature 090, T011/T017): the ``metrics_history`` chat handler.

The handler is where this feature's user-visible promises live: rates in bits/s with the window
stated (FR-005/FR-006), operator shorthand resolving to real device and interface names
(FR-009/FR-010), busiest-first with a cap that accounts for what it dropped (FR-007/FR-008), and —
the one that matters during an outage — an empty result that can never read as zero traffic
(FR-011).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import metricstore
from obi.core.audit import AuditTrail
from obi.core.config import SnmpHistorySettings
from obi.core.drivers.sqlite_metrics import SqliteMetricsHistorySource
from obi.core.metricstore import StoredSample
from obi.core.toolset import ToolContext, _metrics_history
from tests.fakes import chat_config, snmp_settings

_DEVICE = "tamarack-internet-gw"
_DTS = "sfp-sfpplus5-sycamore"


def _now() -> datetime:
    return datetime.now(UTC)


def _sample(
    metric: str,
    ago_s: float,
    value: int,
    *,
    device: str = _DEVICE,
    ifindex: str = "5",
    ifname: str | None = _DTS,
    kind: str = "counter64",
) -> StoredSample:
    t = int((_now() - timedelta(seconds=ago_s)).timestamp() * 1_000_000)
    return StoredSample(
        ts_us=t,
        ts_iso=datetime.fromtimestamp(t / 1_000_000, tz=UTC).isoformat(),
        device=device,
        address="172.18.81.1",
        profile="interfaces" if ifname else "system",
        metric=metric,
        ifindex=ifindex,
        ifname=ifname,
        kind=kind,
        value=value,
    )


def _traffic(
    *, ifindex: str, ifname: str, in_bytes_per_s: int, out_bytes_per_s: int = 0
) -> list[StoredSample]:
    """Two readings 10 s apart on each direction, giving exactly the requested rates."""

    rows: list[StoredSample] = []
    for metric, rate in (("ifHCInOctets", in_bytes_per_s), ("ifHCOutOctets", out_bytes_per_s)):
        rows.append(_sample(metric, 60, 1_000_000, ifindex=ifindex, ifname=ifname))
        rows.append(
            _sample(metric, 50, 1_000_000 + rate * 10, ifindex=ifindex, ifname=ifname)
        )
    return rows


def _ctx(tmp_path: Path, rows: list[StoredSample], *, history: bool = True) -> ToolContext:
    db_path = tmp_path / "metrics.db"
    if rows or history:
        conn = metricstore.connect(db_path)
        metricstore.insert_samples(conn, rows)
        conn.commit()
        conn.close()
    settings = SnmpHistorySettings(db_path=db_path)
    config = chat_config(
        tmp_path, snmp=snmp_settings(history=settings) if history else snmp_settings()
    )
    return ToolContext(
        config=config,
        driver=None,
        audit=AuditTrail(config.audit_path),
        metrics_history_source=SqliteMetricsHistorySource(settings) if history else None,
        question="how much traffic on the sycamore?",
    )


# --- US1: a named interface -------------------------------------------------------------------


def test_reports_both_directions_in_bits_per_second_with_the_window(tmp_path: Path) -> None:
    # 40 MB/s in, 5 MB/s out → 320 Mbit/s and 40 Mbit/s.
    ctx = _ctx(
        tmp_path,
        _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=40_000_000, out_bytes_per_s=5_000_000),
    )
    out = _metrics_history(ctx, {"device": _DEVICE, "interface": "sycamore"})
    assert _DTS in out
    assert "320 Mbit/s" in out and "40 Mbit/s" in out
    assert "in " in out and "out " in out
    assert "UTC" in out  # the window it actually covered
    assert "bytes" not in out.lower()  # operators size circuits in bits


def test_interface_shorthand_resolves_to_the_stored_name(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=1_000_000))
    out = _metrics_history(ctx, {"device": _DEVICE, "interface": "SYCAMORE"})
    assert _DTS in out


def test_device_shorthand_resolves_against_the_devices_that_have_history(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=1_000_000))
    out = _metrics_history(ctx, {"device": "tamarack-internet"}, )
    assert _DTS in out and "No stored history" not in out


def test_ambiguous_device_lists_candidates_instead_of_guessing(tmp_path: Path) -> None:
    rows = _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=1_000_000)
    rows += [
        _sample("ifHCInOctets", 60, 0, device="tamarack-gw", ifindex="1", ifname="ether1"),
        _sample("ifHCInOctets", 50, 10, device="tamarack-gw", ifindex="1", ifname="ether1"),
    ]
    ctx = _ctx(tmp_path, rows)
    out = _metrics_history(ctx, {"device": "tamarack"})
    assert "No stored history" in out
    assert "tamarack-gw" in out and _DEVICE in out


def test_a_named_single_metric_is_reported_in_its_own_unit(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        [
            _sample("cpuLoad", 60, 10, ifindex="", ifname=None, kind="gauge"),
            _sample("cpuLoad", 50, 30, ifindex="", ifname=None, kind="gauge"),
        ],
    )
    out = _metrics_history(ctx, {"device": _DEVICE, "metric": "cpuLoad"})
    assert "cpuLoad" in out
    assert "20 %" in out and "30 %" in out


def test_explicit_window_is_honoured(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=1_000_000))
    out = _metrics_history(ctx, {"device": _DEVICE, "since": "5m"})
    assert _DTS in out
    # A window that ended before the readings were taken finds nothing (and says so).
    stale = _metrics_history(ctx, {"device": _DEVICE, "since": "30d", "until": "20d"})
    assert "no readings" in stale.lower()


def test_backwards_window_is_refused_clearly(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=1_000))
    out = _metrics_history(ctx, {"device": _DEVICE, "since": "1h", "until": "2h"})
    assert "empty" in out.lower()


# --- US2: device-only, busiest first ----------------------------------------------------------


def test_busiest_first_with_a_cap_that_accounts_for_what_it_dropped(tmp_path: Path) -> None:
    rows: list[StoredSample] = []
    for i in range(1, 13):  # 12 interfaces, each busier than the last
        rows += _traffic(ifindex=str(i), ifname=f"ether{i}", in_bytes_per_s=i * 1_000_000)
    ctx = _ctx(tmp_path, rows)
    out = _metrics_history(ctx, {"device": _DEVICE})
    lines = out.splitlines()
    assert lines[1].startswith("- ether12")  # busiest first
    assert sum(1 for line in lines if line.startswith("- ")) == 8  # capped
    assert "4 more interface(s)" in out  # and the remainder is stated, not dropped
    assert "ether1:" not in out


def test_a_long_window_is_no_longer_than_a_short_one(tmp_path: Path) -> None:
    rows: list[StoredSample] = []
    for i in range(1, 13):
        rows += _traffic(ifindex=str(i), ifname=f"ether{i}", in_bytes_per_s=i * 1_000_000)
    ctx = _ctx(tmp_path, rows)
    short = _metrics_history(ctx, {"device": _DEVICE, "since": "10m"})
    long = _metrics_history(ctx, {"device": _DEVICE, "since": "90d"})
    assert len(long.splitlines()) == len(short.splitlines())


def test_an_all_idle_device_says_so_instead_of_listing_zeros(tmp_path: Path) -> None:
    rows: list[StoredSample] = []
    for i in range(1, 6):
        rows += _traffic(ifindex=str(i), ifname=f"ether{i}", in_bytes_per_s=0)
    ctx = _ctx(tmp_path, rows)
    out = _metrics_history(ctx, {"device": _DEVICE})
    assert "No interface carried measurable traffic" in out
    assert out.count("- ether") == 0


def test_idle_interfaces_are_counted_separately_from_unmeasured_ones(tmp_path: Path) -> None:
    rows = _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=1_000_000)
    rows += _traffic(ifindex="6", ifname="ether6", in_bytes_per_s=0)  # measured idle
    rows.append(_sample("ifHCInOctets", 60, 7, ifindex="7", ifname="ether7"))  # one reading only
    ctx = _ctx(tmp_path, rows)
    out = _metrics_history(ctx, {"device": _DEVICE})
    assert "1 interface(s) measured idle (0 bit/s)." in out
    assert "1 interface(s) have NO readings in this window" in out


# --- FR-011: absence is never zero ------------------------------------------------------------


def test_unknown_device_names_the_devices_that_do_have_history(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=1_000))
    out = _metrics_history(ctx, {"device": "tamarack-sw"})
    assert "No stored history" in out
    assert _DEVICE in out
    assert "snmp_metrics" in out  # and points at the tool that CAN read it live
    assert "0 bit/s" not in out


def test_unknown_interface_lists_the_interfaces_that_exist(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=1_000))
    out = _metrics_history(ctx, {"device": _DEVICE, "interface": "ether99"})
    assert "no interface matching" in out
    assert _DTS in out
    assert "0 bit/s" not in out


def test_empty_window_says_missing_data_not_zero_and_dates_the_store(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=1_000))
    out = _metrics_history(ctx, {"device": _DEVICE, "since": "30d", "until": "20d"})
    assert "missing data, not zero traffic" in out
    assert "oldest reading" in out
    assert "0 bit/s" not in out


def test_a_single_reading_is_reported_as_no_readings_never_as_zero(tmp_path: Path) -> None:
    # One cumulative counter value is not a rate — the exact trap snmp_metrics falls into.
    ctx = _ctx(tmp_path, [_sample("ifHCInOctets", 60, 80_970_000_000_000)])
    out = _metrics_history(ctx, {"device": _DEVICE, "interface": "sycamore"})
    assert "no readings" in out.lower()
    assert "80970000000000" not in out and "0 bit/s" not in out


# --- gating -----------------------------------------------------------------------------------


def test_without_history_configured_it_explains_and_points_at_the_live_tool(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path, [], history=False)
    out = _metrics_history(ctx, {"device": _DEVICE})
    assert "[snmp.history]" in out
    assert "snmp_metrics" in out


def test_missing_device_argument_asks_for_one(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _traffic(ifindex="5", ifname=_DTS, in_bytes_per_s=1_000))
    assert "Which device" in _metrics_history(ctx, {"device": "  "})


def test_a_store_that_was_never_created_degrades_cleanly(tmp_path: Path) -> None:
    settings = SnmpHistorySettings(db_path=tmp_path / "never-collected.db")
    config = chat_config(tmp_path, snmp=snmp_settings(history=settings))
    ctx = ToolContext(
        config=config,
        driver=None,
        audit=AuditTrail(config.audit_path),
        metrics_history_source=SqliteMetricsHistorySource(settings),
    )
    out = _metrics_history(ctx, {"device": _DEVICE})
    assert "unavailable" in out.lower()
    assert "obi metrics ingest" in out
