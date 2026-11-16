"""Unit (feature 096): the SNMP health-sensor table reader against recorded live fixtures.

The `health` profile walks the RouterOS v7 mtxrHealth gauge table ONCE
(1.3.6.1.4.1.14988.1.1.3.100.1: name/.2, value/.3, unit/.4) and keys each series by sensor
name. Sensor sets vary per model (CCR2004: temps+fans+PSU states; RB5009: temps+voltages),
so the reader takes what it finds. Normalization at ingest keeps the store's one
convention: °C ×10 (deci-°C), dV/dA/dW/RPM/status raw. Absent table ⇒ absent-with-note,
never zeros (FR-003).
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.drivers.snmp_profiles import get_profile
from obi.core.models import MetricTarget, SnmpOutcome
from tests.fakes import FakeSnmpAgent, snmp_agent_from_fixture, snmp_source

_HEALTH_BASE = "1.3.6.1.4.1.14988.1.1.3.100.1"
_ADDR = "172.18.81.1"


def _fetch(agent: FakeSnmpAgent):
    source = snmp_source({_ADDR: agent})
    target = MetricTarget(address=_ADDR, identity="chestnut-gw", source="mikrotik-roster")
    return source.fetch_metrics(target, profile=get_profile("health"), timeout=5.0)


def _fixture_rows(name: str) -> dict[str, object]:
    path = Path(__file__).parent.parent / "fixtures" / "snmp" / f"{name}.json"
    return json.loads(path.read_text())["walks"][_HEALTH_BASE]


def test_ccr2004_sensors_are_read_and_normalized() -> None:
    rows = _fixture_rows("health-ccr2004")
    report = _fetch(snmp_agent_from_fixture("health-ccr2004"))
    assert report.outcome is SnmpOutcome.REACHABLE
    by_name = {s.name: s for s in report.samples if s.value is not None}

    # Temperatures (unit 1) are stored deci-°C: raw × 10.
    raw_cpu = rows["3.17"]
    assert by_name["health:cpu-temperature"].value == raw_cpu * 10
    # RPM (unit 2) and status (unit 6) stay raw.
    assert by_name["health:fan1-speed"].value == rows["3.7001"]
    assert by_name["health:psu1-state"].value == rows["3.7401"]
    # Series identity: index = sensor row id, label = sensor name.
    assert by_name["health:psu1-state"].index == "7401"
    assert by_name["health:psu1-state"].label == "psu1-state"


def test_rb5009_has_voltages_but_no_fans() -> None:
    rows = _fixture_rows("health-rb5009")
    report = _fetch(snmp_agent_from_fixture("health-rb5009"))
    names = {s.name for s in report.samples}
    assert "health:2pin-voltage" in names  # dV (unit 3) stays raw
    by_name = {s.name: s for s in report.samples}
    assert by_name["health:2pin-voltage"].value == rows["3.7201"]
    assert by_name["health:poe-out-consumption"].value == rows["3.16"]  # dW stays raw
    assert not any(n.startswith("health:fan") for n in names)  # model has no fans


def test_absent_table_is_absent_with_note_not_zero() -> None:
    report = _fetch(FakeSnmpAgent(table={}))
    assert report.outcome is SnmpOutcome.REACHABLE
    assert all(s.value is None for s in report.samples)
    assert any(s.note for s in report.samples)


def test_unknown_unit_code_keeps_raw_value_with_note() -> None:
    agent = FakeSnmpAgent(
        table={_HEALTH_BASE: {"2.99": "mystery-sensor", "3.99": 123, "4.99": 42}}
    )
    report = _fetch(agent)
    sample = next(s for s in report.samples if s.name == "health:mystery-sensor")
    assert sample.value == 123  # never scaled by a guess
    assert sample.note and "unit" in sample.note


def test_row_without_a_name_is_skipped() -> None:
    agent = FakeSnmpAgent(table={_HEALTH_BASE: {"3.50": 34, "4.50": 1}})
    report = _fetch(agent)
    assert report.outcome is SnmpOutcome.REACHABLE
    assert all(s.value is None for s in report.samples)  # nothing storable without identity


def test_health_and_optics_are_single_walk_profiles() -> None:
    """T014: each 096 profile issues exactly ONE walk (whole subtree, no per-column walks),
    so the per-target deadline budget (engine._WALK_BUDGETS = 3) needs no bump — every
    profile's fan-out gets its own deadline and these stay far inside one walk's worth."""

    from obi.core.drivers.snmp import SnmpMetricsSource
    from tests.fakes import snmp_settings

    class CountingTransport:
        def __init__(self) -> None:
            self.walks: list[str] = []

        def get(self, oids):  # noqa: ANN001 - protocol shape
            return {}

        def walk(self, base: str):
            self.walks.append(base)
            return []

    for profile_name in ("health", "optics"):
        transport = CountingTransport()
        source = SnmpMetricsSource(
            snmp_settings(), transport_factory=lambda t, a, p, tr=transport: tr
        )
        target = MetricTarget(address=_ADDR, identity="chestnut-gw", source="mikrotik-roster")
        source.fetch_metrics(target, profile=get_profile(profile_name), timeout=5.0)
        assert len(transport.walks) == 1, (profile_name, transport.walks)
