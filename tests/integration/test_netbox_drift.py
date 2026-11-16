"""T007: netbox_reconcile engine orchestration (feature 017).

The pure classification matrix lives in tests/unit/test_reconcile.py; this covers the engine
wrapper: ONE reconcile_netbox audit record, the two resilience guards (NetBox unreachable / absent
roster → no fabricated drift), and that the run writes nothing to the roster (read-only, SC-007).
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core import inventory as inv
from obi.core.audit import AuditTrail
from obi.core.engine import netbox_reconcile
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import (
    DiscoveryScope,
    DiscoveryState,
    DriftClass,
    NetboxDevice,
    SourceOutcome,
)
from tests.fakes import FakeDocSource

SCOPE = DiscoveryScope(ranges=["172.18.0.0/16"], seeds=["172.18.89.1"])


def _write_roster(path: Path) -> None:
    entries = [
        InventoryEntry(
            address="172.18.86.1", identity="walnut-gw", discovery_state=DiscoveryState.IDENTIFIED
        ),
        InventoryEntry(
            address="172.18.74.1",
            identity="chestnut-internet-gw",
            discovery_state=DiscoveryState.IDENTIFIED,
        ),
    ]
    inv.save(Inventory(scope=SCOPE, entries=entries), path)


def _nb(name: str, ip: str | None) -> NetboxDevice:
    return NetboxDevice(name=name, primary_ip=ip, manufacturer="MikroTik")


def test_reconcile_happy_path_audits_once(tmp_path: Path) -> None:
    roster_path = tmp_path / "inventory.json"
    _write_roster(roster_path)
    audit_path = tmp_path / "audit.jsonl"
    source = FakeDocSource(
        [_nb("walnut-gw", "172.18.86.1/24"), _nb("core-gw-2", "172.18.84.12/24")]
    )

    report = netbox_reconcile(
        source,
        audit=AuditTrail(audit_path),
        source_host="netbox.example.com",
        inventory_path=roster_path,
    )

    assert report.source_outcome is SourceOutcome.REACHABLE
    by_name = {d.name: d.drift_class for d in report.drift}
    assert by_name["walnut-gw"] is DriftClass.MATCHED
    assert by_name["chestnut-internet-gw"] is DriftClass.UNDOCUMENTED
    assert by_name["core-gw-2"] is DriftClass.RETIRE_CANDIDATE

    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    reconcile_records = [r for r in records if r["action"] == "reconcile_netbox"]
    assert len(reconcile_records) == 1  # exactly one audit record per run (FR + Principle V)
    assert reconcile_records[0]["params"]["matched"] == 1
    assert reconcile_records[0]["params"]["undocumented"] == 1
    assert reconcile_records[0]["target"] == "netbox:netbox.example.com"


def test_reconcile_netbox_unreachable_asserts_no_drift(tmp_path: Path) -> None:
    roster_path = tmp_path / "inventory.json"
    _write_roster(roster_path)
    source = FakeDocSource([], outcome=SourceOutcome.UNREACHABLE, detail="could not connect")

    report = netbox_reconcile(
        source,
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        source_host="netbox.example.com",
        inventory_path=roster_path,
    )

    assert report.source_outcome is SourceOutcome.UNREACHABLE
    assert report.drift == []  # the live roster is NOT flagged as all-undocumented
    assert "no drift asserted" in (report.detail or "")


def test_reconcile_absent_roster_asserts_no_retire(tmp_path: Path) -> None:
    # No roster on disk → reality unavailable → documented devices are NOT flagged as retire.
    source = FakeDocSource([_nb("walnut-gw", "172.18.86.1/24")])

    report = netbox_reconcile(
        source,
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        source_host="netbox.example.com",
        inventory_path=tmp_path / "missing.json",
    )

    assert report.drift == []
    assert report.roster_generated_at is None
    assert "no live reality" in (report.detail or "")


def test_reconcile_writes_nothing_to_roster(tmp_path: Path) -> None:
    roster_path = tmp_path / "inventory.json"
    _write_roster(roster_path)
    before = roster_path.read_bytes()

    netbox_reconcile(
        FakeDocSource([_nb("walnut-gw", "172.18.86.1/24")]),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        source_host="h",
        inventory_path=roster_path,
    )

    assert roster_path.read_bytes() == before  # read-only: roster byte-for-byte unchanged
