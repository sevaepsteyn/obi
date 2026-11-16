"""T008: drift report JSON serialization validates against drift-output.schema.json."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema

from obi.core.models import (
    DeviceDrift,
    DiscoveryScope,
    DriftClass,
    DriftReport,
    SourceOutcome,
)
from obi.core.serialize import drift_report_to_json

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2] / "tests" / "schemas" / "drift-output.schema.json"
    ).read_text()
)
T0 = datetime(2026, 6, 25, 6, 0, tzinfo=UTC)
_SCOPE = DiscoveryScope(ranges=["172.18.0.0/16"])


def _report(
    drift: list[DeviceDrift],
    *,
    outcome: SourceOutcome = SourceOutcome.REACHABLE,
    scope: DiscoveryScope | None = _SCOPE,
    roster_at: datetime | None = T0,
    detail: str | None = None,
) -> DriftReport:
    return DriftReport(
        source="netbox.example.com",
        source_outcome=outcome,
        drift=drift,
        started_at=T0,
        finished_at=T0,
        roster_generated_at=roster_at,
        scope=scope,
        detail=detail,
    )


def test_full_report_validates_against_schema() -> None:
    drift = [
        DeviceDrift(
            DriftClass.MATCHED,
            name="walnut-gw",
            addresses=["172.18.86.1"],
            model="RB5009",
            serial="S1",
            site="Alder",
            last_seen=T0,
            matched_on="address",
            notes=["documented IP 172.18.86.9 differs from live addresses ['172.18.86.1']"],
        ),
        DeviceDrift(
            DriftClass.UNDOCUMENTED, name="chestnut-internet-gw", addresses=["172.18.74.1"]
        ),
        DeviceDrift(
            DriftClass.RETIRE_CANDIDATE, name="core-gw-2", addresses=["172.18.84.12"], site="Alder"
        ),
        DeviceDrift(DriftClass.OUTSIDE_SCOPE, name="core-sw-mgmt", addresses=["172.24.2.15"]),
        DeviceDrift(DriftClass.UN_CORRELATABLE, name="aspen-gw-glch", addresses=[]),
    ]
    payload = json.loads(drift_report_to_json(_report(drift)))
    jsonschema.validate(payload, _SCHEMA)
    assert payload["counts"] == {
        "total": 5,
        "matched": 1,
        "undocumented": 1,
        "retire_candidate": 1,
        "outside_scope": 1,
        "un_correlatable": 1,
    }


def test_degraded_report_validates_against_schema() -> None:
    payload = json.loads(
        drift_report_to_json(
            _report(
                [],
                outcome=SourceOutcome.UNREACHABLE,
                roster_at=None,
                scope=None,
                detail="NetBox unavailable — no drift asserted",
            )
        )
    )
    jsonschema.validate(payload, _SCHEMA)
    assert payload["roster_generated_at"] is None
    assert payload["scope"] is None
    assert payload["drift"] == []
    assert payload["counts"]["total"] == 0
