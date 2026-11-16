"""Unit: engine.siklu_status / siklu_links (feature 020).

Concurrent over the roster, ONE audit record per run (mirrors uisp_status), total/resilient (a
mix of reachable + auth + timeout still returns every radio), and credential-free auditing.
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.drivers.siklu import SikluAuthError, SikluDriver, SikluSession
from obi.core.engine import siklu_links, siklu_status
from obi.core.models import LinkState, Reachability
from tests.fakes import (
    RecordingSshSession,
    load_siklu_fixture,
    siklu_driver_from_fixture,
    siklu_settings,
)


def test_siklu_status_pair_one_audit_record(tmp_path: Path) -> None:
    driver, _ = siklu_driver_from_fixture("tamarack-ptp-basswood-70", "basswood-ptp-tamarack-70")
    settings = siklu_settings(hosts=["172.18.89.130", "172.18.83.224"])
    audit = AuditTrail(tmp_path / "audit.jsonl")

    report = siklu_status(driver, settings, audit=audit, reason="why")

    assert len(report.radios) == 2
    assert all(r.reachability is Reachability.REACHABLE for r in report.radios)
    # Sorted by identity then address.
    assert [r.device.identity for r in report.radios] == [
        "basswood-ptp-tamarack-70",
        "tamarack-ptp-basswood-70",
    ]
    # EXACTLY one siklu_status audit record for the whole run, carrying intent + counts.
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    records = [json.loads(ln) for ln in lines if '"siklu_status"' in ln]
    assert len(records) == 1
    rec = records[0]
    assert rec["reason"] == "why"
    assert rec["outcome"] == "ok"
    assert rec["params"]["reachable"] == 2
    assert rec["params"]["total"] == 2
    # Credentials never appear in the trail.
    assert "fakesecret44028f" not in (tmp_path / "audit.jsonl").read_text()


def test_siklu_links_from_report(tmp_path: Path) -> None:
    driver, _ = siklu_driver_from_fixture("tamarack-ptp-basswood-70", "basswood-ptp-tamarack-70")
    settings = siklu_settings(hosts=["172.18.89.130", "172.18.83.224"])
    report = siklu_status(driver, settings, audit=AuditTrail(tmp_path / "audit.jsonl"))
    links = siklu_links(report)
    assert len(links) == 1
    # The qam128/qam32 asymmetry is NOT flagged on the healthy link.
    assert links[0].state is LinkState.UP
    assert links[0].asymmetries == []


def test_siklu_status_mixed_reachability_is_total(tmp_path: Path) -> None:
    tamarack = load_siklu_fixture("tamarack-ptp-basswood-70")

    def factory(host: str, timeout: float) -> SikluSession:
        if host == "172.18.89.130":
            return RecordingSshSession(tamarack)
        if host == "172.25.110.2":
            raise SikluAuthError("bad password")
        raise TimeoutError("no answer")

    driver = SikluDriver(siklu_settings(), session=factory)
    settings = siklu_settings(hosts=["172.18.89.130", "172.25.110.2", "172.25.110.3"])
    report = siklu_status(driver, settings, audit=AuditTrail(tmp_path / "audit.jsonl"))

    by_addr = {r.device.address: r for r in report.radios}
    assert by_addr["172.18.89.130"].reachability is Reachability.REACHABLE
    assert by_addr["172.25.110.2"].reachability is Reachability.AUTH_FAILED
    assert by_addr["172.25.110.3"].reachability is Reachability.TIMED_OUT
    assert report.counts["total"] == 3


def test_siklu_status_empty_roster_clean(tmp_path: Path) -> None:
    driver, _ = siklu_driver_from_fixture()
    settings = siklu_settings(hosts=[])
    audit = AuditTrail(tmp_path / "audit.jsonl")
    report = siklu_status(driver, settings, audit=audit)
    assert report.radios == []
    records = [
        ln for ln in (tmp_path / "audit.jsonl").read_text().splitlines() if '"siklu_status"' in ln
    ]
    assert len(records) == 1  # still one record (clean OK)
    assert json.loads(records[0])["outcome"] == "ok"


def test_siklu_status_name_filter(tmp_path: Path) -> None:
    driver, _ = siklu_driver_from_fixture("tamarack-ptp-basswood-70", "basswood-ptp-tamarack-70")
    settings = siklu_settings(hosts=["172.18.89.130", "172.18.83.224"])
    report = siklu_status(
        driver, settings, audit=AuditTrail(tmp_path / "audit.jsonl"), name_filter="tamarack-ptp"
    )
    assert [r.device.identity for r in report.radios] == ["tamarack-ptp-basswood-70"]
