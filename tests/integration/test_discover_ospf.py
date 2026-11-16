"""T009/T018: end-to-end discover with OSPF resolution over the recorded chestnut-gw fixture — the
resolved router appears at its management IP (SC-006), with audit + provenance (feature 004)."""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import crawl
from obi.core.inventory import load, save
from obi.core.models import DiscoveryScope
from tests.fakes import mikrotik_driver_from_fixture

SCOPE = DiscoveryScope(seeds=["172.18.81.1"], ranges=["172.18.0.0/16"])


def test_resolution_discovers_zelkova_at_management_ip(tmp_path: Path) -> None:
    # The recorded chestnut-gw fixture advertises zelkova-gw only at 169.254.1.4/.2.4 and has the
    # matching OSPF adjacencies (router-id 172.18.92.1).
    driver, _ = mikrotik_driver_from_fixture("chestnut-gw-01")
    inv = crawl(
        SCOPE,
        driver,
        audit=AuditTrail(tmp_path / "on.jsonl"),
        timeout=1.0,
        budget=10.0,
        resolve_link_local_via_ospf=True,
    )
    path = tmp_path / "inventory.json"
    save(inv, path)
    loaded = load(path)
    assert loaded is not None

    by = {e.address: e for e in loaded.entries}
    assert "172.18.92.1" in by  # zelkova-gw, reached only via the resolved router-id (SC-006)
    assert by["172.18.92.1"].provenance.startswith("ospf-peer-of:172.18.81.1@169.254.")


def test_resolution_off_does_not_reach_zelkova(tmp_path: Path) -> None:
    driver, _ = mikrotik_driver_from_fixture("chestnut-gw-01")
    inv = crawl(SCOPE, driver, audit=AuditTrail(tmp_path / "off.jsonl"), timeout=1.0, budget=10.0)
    assert "172.18.92.1" not in {e.address for e in inv.entries}


def test_audit_trail_records_resolution_and_provenance(tmp_path: Path) -> None:
    driver, _ = mikrotik_driver_from_fixture("chestnut-gw-01")
    audit_path = tmp_path / "audit.jsonl"
    crawl(
        SCOPE,
        driver,
        audit=AuditTrail(audit_path),
        timeout=1.0,
        budget=10.0,
        resolve_link_local_via_ospf=True,
    )
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]

    resolves = [r for r in records if r["action"] == "resolve"]
    assert any(r["params"]["router_id"] == "172.18.92.1" for r in resolves)

    probes = [r for r in records if r["action"] == "probe"]
    assert any(
        str(p["params"].get("provenance", "")).startswith("ospf-peer-of:") for p in probes
    )
