"""T010: the SNMP adapter's scalar (system) path against an injected fake transport (feature 012).

Covers value mapping, every per-device outcome, noSuchObject → noted-absent on a REACHABLE device,
and that the community string never appears in the returned report.
"""

from __future__ import annotations

from obi.core.drivers.snmp_profiles import get_profile
from obi.core.models import (
    METRIC_SYS_NAME,
    METRIC_SYS_UPTIME,
    MetricReport,
    MetricTarget,
    SnmpOutcome,
)
from tests.fakes import FakeSnmpAgent, snmp_source, system_agent

SYSTEM = get_profile("system")


def _read(agents: dict[str, FakeSnmpAgent], address: str = "172.18.84.1") -> MetricReport:
    source = snmp_source(agents)
    return source.fetch_metrics(
        MetricTarget(address=address, identity="core-gw-1", source="mikrotik-roster"),
        profile=SYSTEM,
        timeout=1.0,
    )


def _value(report: MetricReport, name: str) -> object:
    return next(s.value for s in report.samples if s.name == name and s.index is None)


def test_system_scalars_map_to_samples() -> None:
    report = _read({"172.18.84.1": system_agent(name="core-gw-1", uptime=999)})

    assert report.outcome is SnmpOutcome.REACHABLE
    assert report.profile == "system"
    assert _value(report, METRIC_SYS_NAME) == "core-gw-1"
    assert _value(report, METRIC_SYS_UPTIME) == 999
    assert _value(report, "sysDescr") == "RouterOS CCR2004"
    assert _value(report, "cpuTemperature") == 47  # MIKROTIK-MIB mtxrHlProcessorTemperature


def test_system_cpu_aggregate_and_hr_memory() -> None:
    from obi.core.models import (
        METRIC_CPU_LOAD_PERCENT,
        METRIC_MEM_FREE_KB,
        METRIC_MEM_TOTAL_KB,
        METRIC_MEM_USED_KB,
    )

    report = _read({"172.18.84.1": system_agent()})

    assert _value(report, METRIC_CPU_LOAD_PERCENT) == 40.0  # avg(hrProcessorLoad 30, 50)
    assert _value(report, METRIC_MEM_TOTAL_KB) == 262144  # 262144 units × 1024 / 1024
    assert _value(report, METRIC_MEM_USED_KB) == 131072
    assert _value(report, METRIC_MEM_FREE_KB) == 131072


def test_unreachable_when_no_agent() -> None:
    report = _read({})  # the fake factory raises UNREACHABLE for an unknown address
    assert report.outcome is SnmpOutcome.UNREACHABLE
    assert report.samples == []


def test_distinct_failure_outcomes() -> None:
    for outcome in (SnmpOutcome.AUTH_FAILED, SnmpOutcome.TIMED_OUT, SnmpOutcome.UNREACHABLE):
        report = _read({"172.18.84.1": FakeSnmpAgent(fail=outcome)})
        assert report.outcome is outcome
        assert report.samples == []


def test_no_such_object_is_noted_absent_on_reachable_device() -> None:
    # The agent answers, but does not support sysName (None) — the device stays REACHABLE and that
    # one metric is absent-with-note; the others still read.
    agent = system_agent()
    agent.scalars["1.3.6.1.2.1.1.5.0"] = None
    report = _read({"172.18.84.1": agent})

    assert report.outcome is SnmpOutcome.REACHABLE
    sys_name = next(s for s in report.samples if s.name == METRIC_SYS_NAME)
    assert sys_name.value is None
    assert sys_name.note == "unavailable"
    assert _value(report, METRIC_SYS_UPTIME) == 123456  # the rest still read


def test_community_string_never_appears_in_report() -> None:
    report = _read({"172.18.84.1": system_agent()})
    assert "supersecret" not in repr(report)
    assert "SNMP-COMMUNITY" not in repr(report)


def test_device_status_projection_from_system_report() -> None:
    from obi.core.models import Reachability, device_status_from_metrics

    report = _read({"172.18.84.1": system_agent(name="core-gw-1", uptime=100_000)})
    status = device_status_from_metrics(report)

    assert status.reachability is Reachability.REACHABLE
    assert status.device.identity == "core-gw-1"
    assert status.uptime_seconds == 1000  # timeticks (100_000) / 100
    assert status.cpu_load_percent == 40.0  # avg(hrProcessorLoad 30, 50)
    assert status.total_memory_bytes == 262144 * 1024
    assert status.free_memory_bytes == 131072 * 1024
