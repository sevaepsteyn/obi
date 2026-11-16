"""Unit (feature 096): the SNMP optics (SFP DDM) table reader against recorded fixtures.

The `optics` profile walks mtxrOptical ONCE (1.3.6.1.4.1.14988.1.1.19.1.1). Verified live
quirks under test: a module row whose temperature AND supply voltage are both zero answers
no diagnostics — it must yield NO samples (never stored zeros), only a note; power values
are signed milli-dBm (T007-confirmed ÷1000); the name column is the series label.
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.drivers.snmp_profiles import get_profile
from obi.core.models import MetricTarget, SnmpOutcome
from tests.fakes import FakeSnmpAgent, snmp_agent_from_fixture, snmp_source

_OPTICS_BASE = "1.3.6.1.4.1.14988.1.1.19.1.1"
_ADDR = "172.18.81.1"


def _fetch(agent: FakeSnmpAgent):
    source = snmp_source({_ADDR: agent})
    target = MetricTarget(address=_ADDR, identity="chestnut-gw", source="mikrotik-roster")
    return source.fetch_metrics(target, profile=get_profile("optics"), timeout=5.0)


def _fixture_rows(name: str) -> dict[str, object]:
    path = Path(__file__).parent.parent / "fixtures" / "snmp" / f"{name}.json"
    return json.loads(path.read_text())["walks"][_OPTICS_BASE]


def test_ddm_module_yields_signed_mdbm_and_labels() -> None:
    rows = _fixture_rows("health-ccr2004")
    report = _fetch(snmp_agent_from_fixture("health-ccr2004"))
    assert report.outcome is SnmpOutcome.REACHABLE
    # Row index 2 is a live 850nm module on the recorded device.
    row2 = {s.name: s for s in report.samples if s.index == "2"}
    assert row2["optics:rxPowerMdbm"].value == rows["10.2"]  # raw signed mdBm
    assert row2["optics:rxPowerMdbm"].value < 0
    assert row2["optics:txPowerMdbm"].value == rows["9.2"]
    assert row2["optics:temperature"].value == rows["6.2"] * 10  # °C ×10 at ingest
    assert row2["optics:supplyVoltageMv"].value == rows["7.2"]  # mV raw
    assert row2["optics:txBiasMa"].value == rows["8.2"]  # mA raw
    assert row2["optics:rxLoss"].value == rows["3.2"]
    assert row2["optics:txFault"].value == rows["4.2"]
    assert row2["optics:rxPowerMdbm"].label == rows["2.2"]  # port name labels the series


def test_all_zero_row_is_non_ddm_no_samples_stored() -> None:
    """core-gw-1's sfp-sfpplus1: every DDM column zero ⇒ the module answers no diagnostics.
    Zero mV supply is physically impossible on a live module — storing those zeros would
    fabricate readings (FR-002/FR-003)."""

    report = _fetch(snmp_agent_from_fixture("optics-zero-row"))
    assert report.outcome is SnmpOutcome.REACHABLE
    assert [s for s in report.samples if s.value is not None] == []
    assert any("no DDM" in note for note in report.notes)


def test_mixed_rows_keep_ddm_and_drop_zero() -> None:
    agent = FakeSnmpAgent(
        table={
            _OPTICS_BASE: {
                # live DDM module on row 5
                "2.5": "sfp-sfpplus5", "3.5": 0, "4.5": 0, "6.5": 34, "7.5": 3225,
                "8.5": 6, "9.5": -1907, "10.5": -1656,
                # non-DDM module on row 6
                "2.6": "sfp-sfpplus6", "3.6": 0, "4.6": 0, "6.6": 0, "7.6": 0,
                "8.6": 0, "9.6": 0, "10.6": 0,
            }
        }
    )
    report = _fetch(agent)
    stored = {(s.name, s.index) for s in report.samples if s.value is not None}
    assert ("optics:rxPowerMdbm", "5") in stored
    assert not any(index == "6" for _, index in stored)
    assert any("sfp-sfpplus6" in note for note in report.notes)


def test_no_optics_table_is_clean_absence() -> None:
    report = _fetch(FakeSnmpAgent(table={}))
    assert report.outcome is SnmpOutcome.REACHABLE
    assert [s for s in report.samples if s.value is not None] == []


def test_partial_ddm_drops_unmeasured_power_keeps_environment() -> None:
    """Seen live on tamarack-gw (T018): a DAC reports real temperature/voltage but BOTH power
    columns exactly 0 mdBm — the platform's "not measured" signature. The power metrics (and
    the meaningless bias) must not be stored as zeros; temp/voltage/flags stay."""

    agent = FakeSnmpAgent(
        table={
            _OPTICS_BASE: {
                "2.7": "qsfpplus1-1", "3.7": 0, "4.7": 0, "6.7": 36, "7.7": 3286,
                "8.7": 0, "9.7": 0, "10.7": 0,
            }
        }
    )
    report = _fetch(agent)
    stored = {s.name for s in report.samples if s.value is not None}
    assert "optics:temperature" in stored
    assert "optics:supplyVoltageMv" in stored
    assert "optics:rxLoss" in stored
    assert not ({"optics:txPowerMdbm", "optics:rxPowerMdbm", "optics:txBiasMa"} & stored)
