"""001 status serializer conforms to 001's status-output.schema.json (SC-005).

The CLI-level `status --json` conformance moved to feature 002's test_status_schema.py, since
`obi status` now reads the persisted inventory and emits the 002 (superset) status schema.
These tests still cover the 001 ``report_to_*`` serializer directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema

from obi.core.models import (
    Device,
    DeviceStatus,
    DiscoveryScope,
    InventoryReport,
    Reachability,
)
from obi.core.serialize import report_to_dict, report_to_json

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "schemas" / "mikrotik-status-output.schema.json"
)


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())


def _diverse_report() -> InventoryReport:
    now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)
    statuses = [
        DeviceStatus(
            Device("border-gw-01", "172.18.81.1", model="RB750Gr3", version="7.11.2"),
            Reachability.REACHABLE,
            uptime_seconds=123456,
            cpu_load_percent=7.0,
            free_memory_bytes=234123264,
            total_memory_bytes=268435456,
            gathered_at=now,
        ),
        DeviceStatus(Device("", "172.18.81.2"), Reachability.UNREACHABLE, gathered_at=now),
        DeviceStatus(Device("dup", "172.18.81.3"), Reachability.AUTH_FAILED, gathered_at=now),
        DeviceStatus(Device("dup", "172.18.81.4"), Reachability.TIMED_OUT, gathered_at=now),
        DeviceStatus(Device("", "172.18.81.5"), Reachability.UNIDENTIFIED, gathered_at=now),
    ]
    return InventoryReport(
        scope=DiscoveryScope(ranges=["172.18.81.0/24"], seeds=["172.18.81.1"]),
        statuses=statuses,
        started_at=now,
        finished_at=now,
    )


def test_serialized_report_matches_schema() -> None:
    payload = report_to_dict(_diverse_report())
    jsonschema.validate(instance=payload, schema=_schema())


def test_json_is_valid_and_parseable() -> None:
    text = report_to_json(_diverse_report())
    parsed = json.loads(text)  # must round-trip through a standard JSON tool (SC-005)
    assert parsed["counts"]["total"] == 5
    assert {d["address"] for d in parsed["devices"]} == {
        "172.18.81.1",
        "172.18.81.2",
        "172.18.81.3",
        "172.18.81.4",
        "172.18.81.5",
    }
    # Duplicate/blank identities coexist, disambiguated by address (edge case).
    dups = [d for d in parsed["devices"] if d["identity"] == "dup"]
    assert {d["address"] for d in dups} == {"172.18.81.3", "172.18.81.4"}
    blanks = [d for d in parsed["devices"] if d["identity"] is None]
    assert len(blanks) == 2
