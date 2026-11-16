"""T011: SNMP target resolution + the snmp_metrics orchestration/audit (feature 012).

Covers Mikrotik + NetBox target resolution (mask strip, IPv6, skip-with-note, no cross-vendor
merge) and that snmp_metrics writes one credential-free `snmp_metrics` audit record per target.
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.config import Config
from obi.core.engine import resolve_snmp_targets, snmp_metrics
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import DiscoveryScope, DiscoveryState, NetboxDevice
from tests.fakes import (
    FakeDocSource,
    FakeMetricsSource,
    netbox_settings,
    save_inventory,
    snmp_settings,
)


def _roster() -> Inventory:
    return Inventory(
        scope=DiscoveryScope(seeds=["172.18.84.1"]),
        entries=[
            InventoryEntry(address="172.18.84.1", identity="core-gw-1",
                           discovery_state=DiscoveryState.IDENTIFIED),
            InventoryEntry(address="172.18.84.9", identity="switch-1",
                           discovery_state=DiscoveryState.UNSUPPORTED),  # not SNMP-eligible
        ],
    )


def test_resolve_mikrotik_targets_only_identified() -> None:
    targets, notes = resolve_snmp_targets(
        inventory=_roster(), netbox_devices=[], source="mikrotik"
    )
    assert [t.address for t in targets] == ["172.18.84.1"]  # the UNSUPPORTED entry is excluded
    assert targets[0].source == "mikrotik-roster"
    assert notes == []


def test_resolve_netbox_strips_mask_handles_ipv6_and_skips_no_ip() -> None:
    devices = [
        NetboxDevice(name="edge-1", primary_ip="172.18.84.5/24"),
        NetboxDevice(name="edge-6", primary_ip="2001:db8::1/64"),
        NetboxDevice(name="no-ip", primary_ip=None),
    ]
    targets, notes = resolve_snmp_targets(
        inventory=None, netbox_devices=devices, source="netbox"
    )
    addresses = {t.address for t in targets}
    assert addresses == {"172.18.84.5", "2001:db8::1"}  # masks stripped, IPv6 kept
    assert all(t.source == "netbox" for t in targets)
    assert any("no-ip" in note for note in notes)  # skipped with a note, not a failure


def test_same_address_deduped_with_unioned_sources() -> None:
    # The same literal address from two sources is one SNMP endpoint: one target, both sources
    # labeled (not two queries) — and NOT cross-vendor identity matching of different addresses.
    inventory = Inventory(
        scope=DiscoveryScope(seeds=["172.18.84.1"]),
        entries=[InventoryEntry(address="172.18.84.5", identity="core-gw-1",
                                discovery_state=DiscoveryState.IDENTIFIED)],
    )
    devices = [NetboxDevice(name="core-gw-1", primary_ip="172.18.84.5/24")]
    targets, _notes = resolve_snmp_targets(
        inventory=inventory, netbox_devices=devices, source="all"
    )
    same = [t for t in targets if t.address == "172.18.84.5"]
    assert len(same) == 1  # deduped to one endpoint — polled once, audited once
    assert set(same[0].source.split("+")) == {"mikrotik-roster", "netbox"}  # both sources labeled


def test_distinct_addresses_are_not_merged() -> None:
    # Dedup is exact-address only: different addresses stay separate (no identity correlation).
    inventory = Inventory(
        scope=DiscoveryScope(seeds=["172.18.84.1"]),
        entries=[InventoryEntry(address="172.18.84.5", identity="core-gw-1",
                                discovery_state=DiscoveryState.IDENTIFIED)],
    )
    devices = [NetboxDevice(name="core-gw-1", primary_ip="172.18.84.6/24")]  # different IP
    targets, _notes = resolve_snmp_targets(
        inventory=inventory, netbox_devices=devices, source="all"
    )
    assert {t.address for t in targets} == {
        "172.18.84.5",
        "172.18.84.6",
    }  # both kept, not correlated


def _config(tmp_path: Path) -> Config:
    save_inventory(
        tmp_path / "inventory.json",
        [InventoryEntry(address="172.18.84.1", identity="core-gw-1",
                        discovery_state=DiscoveryState.IDENTIFIED)],
    )
    from tests.fakes import settings

    return Config(
        scope=DiscoveryScope(seeds=["172.18.84.1"]),
        routeros=settings(),
        snmp=snmp_settings(),
        audit_path=tmp_path / "audit.jsonl",
        inventory_path=tmp_path / "inventory.json",
    )


def test_snmp_metrics_audits_one_query_snmp_per_target_credential_free(tmp_path: Path) -> None:
    config = _config(tmp_path)
    audit = AuditTrail(config.audit_path)
    source = FakeMetricsSource()

    reports = snmp_metrics(
        source, config=config, target="all", profile_name="system", source="mikrotik",
        audit=audit, reason="is core up over snmp?",
    )

    assert [r.target.address for r in reports] == ["172.18.84.1"]
    assert source.calls and source.calls[0][0] == "172.18.84.1"  # the engine consulted the source

    records = [json.loads(line) for line in config.audit_path.read_text().splitlines()]
    snmp_records = [r for r in records if r["action"] == "snmp_metrics"]
    assert len(snmp_records) == 1
    record = snmp_records[0]
    assert record["target"] == "172.18.84.1"
    assert record["reason"] == "is core up over snmp?"
    assert record["outcome"] == "ok"
    assert record["params"]["profile"] == "system"
    assert "supersecret" not in config.audit_path.read_text()


def test_snmp_metrics_netbox_targets_via_source(tmp_path: Path) -> None:
    config = Config(
        netbox=netbox_settings(),
        snmp=snmp_settings(),
        audit_path=tmp_path / "audit.jsonl",
        inventory_path=tmp_path / "inventory.json",
    )
    netbox_source = FakeDocSource([NetboxDevice(name="edge-1", primary_ip="172.18.84.5/24")])
    reports = snmp_metrics(
        FakeMetricsSource(), config=config, target="all", profile_name="system",
        source="netbox", audit=AuditTrail(config.audit_path), netbox_source=netbox_source,
    )
    assert [r.target.address for r in reports] == ["172.18.84.5"]
