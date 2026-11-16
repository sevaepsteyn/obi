"""Contract: Siklu JSON output validates against siklu-status-output.schema.json (feature 020)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from obi.core.audit import AuditTrail
from obi.core.engine import siklu_links, siklu_status
from obi.core.models import SikluReport
from obi.core.serialize import siklu_report_to_dict
from tests.fakes import siklu_driver_from_fixture, siklu_settings

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "tests" / "schemas" / "siklu-status-output.schema.json"
    ).read_text()
)


def _report(tmp_path: Path, *, links: bool) -> SikluReport:
    driver, _ = siklu_driver_from_fixture("tamarack-ptp-basswood-70", "basswood-ptp-tamarack-70")
    settings = siklu_settings(hosts=["172.18.89.130", "172.18.83.224"])
    report = siklu_status(driver, settings, audit=AuditTrail(tmp_path / "audit.jsonl"))
    if links:
        report.links = siklu_links(report, overrides=settings.pairs)
    return report


def test_radio_report_conforms(tmp_path: Path) -> None:
    payload = siklu_report_to_dict(_report(tmp_path, links=False))
    jsonschema.validate(instance=payload, schema=_SCHEMA)
    assert {r["vendor"] for r in payload["radios"]} == {"siklu"}
    assert "links" not in payload  # omitted in the plain radio view


def test_link_report_conforms(tmp_path: Path) -> None:
    payload = siklu_report_to_dict(_report(tmp_path, links=True))
    jsonschema.validate(instance=payload, schema=_SCHEMA)
    assert len(payload["links"]) == 1
    link = payload["links"][0]
    assert link["state"] == "up"
    assert link["paired_by"] == "name"
    # The qam128/qam32 per-direction difference is NOT flagged as asymmetry.
    assert link["asymmetries"] == []
    assert link["end_a"]["vendor"] == "siklu"
