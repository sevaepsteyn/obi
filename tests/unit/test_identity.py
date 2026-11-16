"""T025: duplicate/blank identity disambiguation (edge case, FR-017)."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.models import Device, DiscoveryScope
from tests.fakes import FakeBehavior, FakeDriver
from tests.inventory_helpers import run_inventory


def _audit(tmp_path: Path) -> AuditTrail:
    return AuditTrail(tmp_path / "audit.jsonl")


def test_duplicate_identity_both_listed(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(device=Device("samename", "172.25.110.1")),
            "172.25.110.2": FakeBehavior(device=Device("samename", "172.25.110.2")),
        }
    )
    scope = DiscoveryScope(seeds=["172.25.110.1", "172.25.110.2"])

    report = run_inventory(scope, driver, audit=_audit(tmp_path), timeout=1.0, concurrency=4)

    assert len(report.statuses) == 2  # neither merged nor dropped
    assert {s.device.address for s in report.statuses} == {"172.25.110.1", "172.25.110.2"}
    assert all(s.device.identity == "samename" for s in report.statuses)


def test_blank_identity_disambiguated_by_address(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(device=Device("", "172.25.110.1")),
            "172.25.110.2": FakeBehavior(device=Device("", "172.25.110.2")),
        }
    )
    scope = DiscoveryScope(seeds=["172.25.110.1", "172.25.110.2"])

    report = run_inventory(scope, driver, audit=_audit(tmp_path), timeout=1.0, concurrency=4)

    assert len(report.statuses) == 2
    assert {s.device.address for s in report.statuses} == {"172.25.110.1", "172.25.110.2"}
    assert all(s.device.identity == "" for s in report.statuses)
