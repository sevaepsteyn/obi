"""Integration: `obi web export` merges the Siklu e-band hop (044 US4).

Fixture UISP fleet + the recorded 020 radio captures → the export carries one
``source: "siklu"`` link in the 70 GHz band, schema-valid, audited; an unreachable Siklu
fleet degrades to a named exclusion and the export still succeeds with the UISP links.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from obi.core.audit import AuditTrail
from obi.core.config import WebSettings
from obi.core.drivers.siklu import parse_rf, parse_system
from obi.core.engine import ar_export, siklu_links
from obi.core.models import (
    Device,
    GroundProvenance,
    Reachability,
    SikluRadioStatus,
    SikluReport,
)
from tests.fakes import geometry_snapshot, load_siklu_fixture

SCHEMA_PATH = (
    Path(__file__).parents[2]
    / "tests" / "schemas" / "ar-export.schema.json"
)


class FakeGeometrySource:
    def geometry(self, *, timeout: float) -> Any:
        return geometry_snapshot()


class FakeElevation:
    def ground(self, site_name: str, lat: float, lon: float) -> tuple[float, GroundProvenance]:
        return 1190.66, GroundProvenance.CACHE


def _fixture_report(*, reachable: bool = True) -> SikluReport:
    radios = []
    for name in ("tamarack-ptp-basswood-70", "basswood-ptp-tamarack-70"):
        commands = load_siklu_fixture(name)
        radios.append(
            SikluRadioStatus(
                device=Device(identity=name, address=f"{name}.net.example.org"),
                reachability=Reachability.REACHABLE if reachable else Reachability.UNREACHABLE,
                location=parse_system(commands["show system"]).get("location"),
                rf=parse_rf(commands["show rf"]) if reachable else None,
            )
        )
    now = datetime.now(UTC)
    report = SikluReport(radios=radios, started_at=now, finished_at=now)
    report.links = siklu_links(report)
    return report


def _export(tmp_path: Path, report: SikluReport):
    out_path = tmp_path / "links.json"
    geometry = ar_export(
        FakeGeometrySource(),  # type: ignore[arg-type]
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        controller="https://uisp.example.com",
        settings=WebSettings(),
        elevation=FakeElevation(),  # type: ignore[arg-type]
        out_path=out_path,
        siklu=report,
        timeout=5.0,
    )
    return geometry, json.loads(out_path.read_text()), tmp_path / "audit.jsonl"


def test_export_merges_the_eband_hop(tmp_path: Path) -> None:
    _geometry, payload, audit_path = _export(tmp_path, _fixture_report())
    jsonschema.validate(payload, json.loads(SCHEMA_PATH.read_text()))

    siklu = [link for link in payload["links"] if link["source"] == "siklu"]
    assert len(siklu) == 1
    [link] = siklu
    assert link["band"] == "70 GHz"
    assert link["capacity_down_bps"] and link["capacity_down_bps"] >= 1_000_000_000
    assert link["frequency_ghz"] and 70 <= link["frequency_ghz"] <= 90
    assert link["fresnel_radius_m"] and link["fresnel_radius_m"] > 0
    assert link["maintenance"] is False

    # both radio endpoints exported and present in every site's ENU table
    endpoint_names = {e["name"] for e in payload["endpoints"]}
    assert {"tamarack-ptp-basswood-70", "basswood-ptp-tamarack-70"} <= endpoint_names
    for site in payload["sites"]:
        assert "tamarack-ptp-basswood-70" in site["enu"]

    # the one ar_export audit record carries the merge counts
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    [export_record] = [r for r in records if r["action"] == "ar_export"]
    assert export_record["params"]["siklu"] == {"merged": 1, "excluded": 0}


def test_unreachable_siklu_degrades_to_a_named_exclusion(tmp_path: Path) -> None:
    geometry, payload, audit_path = _export(tmp_path, _fixture_report(reachable=False))

    assert not [link for link in payload["links"] if link["source"] == "siklu"]
    assert payload["links"], "the UISP links still export"
    assert any("siklu" in item["link"] for item in payload["excluded"])
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    [export_record] = [r for r in records if r["action"] == "ar_export"]
    assert export_record["outcome"] == "ok"
    assert export_record["params"]["siklu"] == {"merged": 0, "excluded": 1}
