"""T020 (US2): the SNMP adapter's interface table walk against a fake transport (feature 012).

Covers one row per interface (indexed by ifIndex, labeled by ifName), the 64→32-bit counter
fallback with its note, and a large table bounded with a truncation note.
"""

from __future__ import annotations

from obi.core.drivers.snmp import _MAX_TABLE_ROWS
from obi.core.drivers.snmp_profiles import get_profile
from obi.core.models import MetricReport, MetricSample, MetricTarget, SnmpOutcome
from tests.fakes import FakeSnmpAgent, snmp_source

INTERFACES = get_profile("interfaces")


def _read(agent: FakeSnmpAgent) -> MetricReport:
    return snmp_source({"172.18.84.1": agent}).fetch_metrics(
        MetricTarget(address="172.18.84.1", identity="core-gw-1", source="mikrotik-roster"),
        profile=INTERFACES,
        timeout=1.0,
    )


def _row(report: MetricReport, index: str, name: str) -> MetricSample:
    return next(s for s in report.samples if s.index == index and s.name == name)


def test_one_row_per_interface_labeled_by_ifname() -> None:
    agent = FakeSnmpAgent(
        table={
            "1.3.6.1.2.1.31.1.1.1.1": {"1": "ether1", "2": "ether2"},
            "1.3.6.1.2.1.2.2.1.8": {"1": 1, "2": 2},
            "1.3.6.1.2.1.31.1.1.1.6": {"1": 1000, "2": 2000},
            "1.3.6.1.2.1.31.1.1.1.10": {"1": 500, "2": 700},
        }
    )
    report = _read(agent)

    assert report.outcome is SnmpOutcome.REACHABLE
    assert _row(report, "1", "ifHCInOctets").value == 1000
    assert _row(report, "1", "ifHCInOctets").label == "ether1"
    assert _row(report, "2", "ifOperStatus").value == 2
    assert _row(report, "2", "ifHCOutOctets").label == "ether2"


def test_64bit_counter_falls_back_to_32bit_with_note() -> None:
    # ether1 has no 64-bit ifHCInOctets, only the 32-bit ifInOctets sibling.
    agent = FakeSnmpAgent(
        table={
            "1.3.6.1.2.1.31.1.1.1.1": {"1": "ether1"},
            "1.3.6.1.2.1.2.2.1.8": {"1": 1},
            "1.3.6.1.2.1.2.2.1.10": {"1": 42},  # ifInOctets (32-bit fallback)
        }
    )
    report = _read(agent)

    sample = _row(report, "1", "ifHCInOctets")
    assert sample.value == 42
    assert sample.note == "32-bit fallback"


def test_large_table_is_truncated_with_a_note() -> None:
    count = _MAX_TABLE_ROWS + 5
    names = {str(i): f"ether{i}" for i in range(1, count + 1)}
    agent = FakeSnmpAgent(table={"1.3.6.1.2.1.31.1.1.1.1": names})
    report = _read(agent)

    indices = {s.index for s in report.samples}
    assert len(indices) == _MAX_TABLE_ROWS
    assert any("truncated" in note for note in report.notes)
