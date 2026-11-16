"""Unit (feature 100): the ``query_health`` chat handler.

The promises under test: fleet mode is worst-first and bounded (breach cap with an honest
"…and N more"); device mode reports newest readings with units and ages, breaches with
limits, staleness marked; verdicts come from the SHARED 096 evaluation (thresholds from
``[watch.health]`` when set); absence never renders as a number; an empty store names the
collection commands; and a dispatched call lands one feature-042 tool event.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core import auditstore, metricstore
from obi.core.audit import AuditRecorder, AuditTrail, audit_scope
from obi.core.config import AuditSettings, SnmpHistorySettings, WatchHealthSettings
from obi.core.metricstore import StoredSample
from obi.core.toolset import ToolContext, _query_health, build_dispatch, build_tools
from tests.fakes import chat_config, snmp_settings


def _sample(
    device: str,
    metric: str,
    value: int,
    *,
    ago_s: float = 60,
    ifindex: str = "",
    ifname: str | None = None,
    profile: str = "health",
) -> StoredSample:
    # Ages are relative to REAL now at call time — a module-import stamp would drift by
    # minutes in a full-suite run and break the age/staleness assertions.
    t = int((datetime.now(UTC) - timedelta(seconds=ago_s)).timestamp() * 1_000_000)
    return StoredSample(
        ts_us=t,
        ts_iso=datetime.fromtimestamp(t / 1_000_000, tz=UTC).isoformat(),
        device=device,
        address="172.18.81.1",
        profile=profile,
        metric=metric,
        ifindex=ifindex,
        ifname=ifname,
        kind="gauge",
        value=value,
    )


def _ctx(tmp_path: Path, rows: list[StoredSample], *, store: bool = True) -> ToolContext:
    db_path = tmp_path / "metrics.db"
    if store:
        conn = metricstore.connect(db_path)
        metricstore.insert_samples(conn, rows)
        conn.commit()
        conn.close()
    config = chat_config(
        tmp_path, snmp=snmp_settings(history=SnmpHistorySettings(db_path=db_path))
    )
    return ToolContext(
        config=config,
        driver=None,
        audit=AuditTrail(config.audit_path),
        question="any hardware problems?",
    )


def _healthy_fleet() -> list[StoredSample]:
    return [
        _sample("chestnut-gw", "health:cpu-temperature", 350, ifindex="17",
                ifname="cpu-temperature"),
        _sample("chestnut-gw", "health:psu1-state", 0, ifindex="7401", ifname="psu1-state"),
        _sample("chestnut-gw", "cpuLoad", 4, profile="system"),
        _sample("core-vms-1", "linux:diskUsedPercent", 423, ifindex="/", ifname="/",
                profile="linux-health"),
    ]


# --- fleet mode -------------------------------------------------------------------------------


def test_fleet_mode_reports_breaches_first_then_stale_then_healthy(tmp_path: Path) -> None:
    rows = _healthy_fleet() + [
        _sample("hot-gw", "health:board-temperature2", 810, ifindex="7102",
                ifname="board-temperature2"),
        _sample("sad-gw", "health:psu2-state", 1, ifindex="7402", ifname="psu2-state"),
        _sample("sequoia-gw", "health:cpu-temperature", 400, ago_s=7200, ifindex="17",
                ifname="cpu-temperature"),
    ]
    out = _query_health(_ctx(tmp_path, rows), {})
    assert "hot-gw" in out and "81" in out and "°C" in out and "75" in out
    assert "sad-gw" in out and "psu2-state" in out
    assert "sequoia-gw" in out and "stale" in out.lower()
    assert "chestnut-gw" in out  # healthy devices appear as a compact roll
    assert out.index("hot-gw") < out.index("chestnut-gw")  # worst first
    assert "2 breaching" in out  # header counts


def test_fleet_mode_all_healthy_says_so(tmp_path: Path) -> None:
    out = _query_health(_ctx(tmp_path, _healthy_fleet()), {})
    assert "0 breaching" in out
    assert "within" in out.lower() or "no breaches" in out.lower()


def test_fleet_mode_caps_breach_lines_and_total_size(tmp_path: Path) -> None:
    """SC-003: the worst-case fleet payload stays inside the radio's budget headroom."""

    rows: list[StoredSample] = []
    for i in range(14):
        rows.append(
            _sample(f"melting-gw-{i:02d}", "health:board-temperature1", 900 + i,
                    ifindex="7101", ifname="board-temperature1")
        )
    out = _query_health(_ctx(tmp_path, rows), {})
    assert out.count("melting-gw") <= 9  # ≤8 breach lines (+1 possible roll mention max)
    assert "6 more" in out  # 14 breaching − 8 shown, honestly noted
    assert len(out) <= 1600


# --- device mode ------------------------------------------------------------------------------


def test_device_mode_groups_readings_with_units_ages_and_breaches(tmp_path: Path) -> None:
    rows = _healthy_fleet() + [
        _sample("chestnut-gw", "optics:rxPowerMdbm", -1907, ifindex="2",
                ifname="sfp-sfpplus1", profile="optics"),
        _sample("chestnut-gw", "health:board-temperature2", 810, ifindex="7102",
                ifname="board-temperature2"),
    ]
    out = _query_health(_ctx(tmp_path, rows), {"device": "chestnut-gw"})
    assert "35 °C" in out or "35.0 °C" in out  # deci-°C scaled for display
    assert "-1.907 dBm" in out
    assert "psu1-state" in out and ("ok" in out.lower())
    assert "board-temperature2" in out and "75" in out  # the breach and its limit
    import re

    assert re.search(r"\(\d+[smh]\)", out)  # every reading carries its age
    assert "disk" not in out.lower()  # class the device does not expose stays absent


def test_device_mode_marks_staleness_with_age(tmp_path: Path) -> None:
    rows = [
        _sample("sequoia-gw", "health:cpu-temperature", 400, ago_s=7200, ifindex="17",
                ifname="cpu-temperature"),
    ]
    out = _query_health(_ctx(tmp_path, rows), {"device": "sequoia-gw"})
    assert "stale" in out.lower()
    assert "2h" in out
    assert "40 °C" in out or "40.0 °C" in out  # last known value, clearly aged


def test_unknown_device_is_honest_and_shorthand_resolves(tmp_path: Path) -> None:
    rows = _healthy_fleet()
    miss = _query_health(_ctx(tmp_path, rows), {"device": "nonexistent-gw"})
    assert "no health" in miss.lower()
    assert "°C" not in miss  # never a fabricated reading

    shorthand = _query_health(_ctx(tmp_path, rows), {"device": "chestnut"})
    assert "chestnut-gw" in shorthand and "35" in shorthand  # unique substring resolves


def test_thresholds_come_from_watch_health_settings(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _healthy_fleet())
    ctx.config.watch.health = WatchHealthSettings(enabled=True, temperature_max_c=30.0)
    out = _query_health(ctx, {"device": "chestnut-gw"})
    assert "30" in out  # the configured limit is what the verdict cites
    assert "cpu-temperature" in out  # 35 °C now breaches


def test_empty_store_names_the_collection_commands(tmp_path: Path) -> None:
    out = _query_health(_ctx(tmp_path, [], store=False), {})
    assert "obi metrics ingest" in out
    assert "obi health probe" in out


# --- audit (FR-005 / SC-004) ------------------------------------------------------------------


def test_dispatched_call_lands_one_tool_event(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _healthy_fleet())
    tools = [t for t in build_tools() if t.name == "query_health"]
    assert tools, "query_health must be a registered tool"
    dispatch = build_dispatch(ctx, tools)
    recorder = AuditRecorder(
        AuditSettings(enabled=True, db_path=tmp_path / "audit.db", retention_days=90,
                      max_payload_bytes=5000)
    )
    with audit_scope(recorder, "chat", "chat-test"):
        dispatch["query_health"]({})
    recorder.close()
    conn = auditstore.connect_readonly(tmp_path / "audit.db")
    try:
        rows, _ = auditstore.query_events(conn, limit=100, include_payload=True)
    finally:
        conn.close()
    tool_events = [r for r in rows if r["kind"] == "tool_call"]
    assert len(tool_events) == 1
    assert "query_health" in str(tool_events[0])
