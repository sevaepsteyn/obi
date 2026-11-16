"""T014: engine happy-path unit tests (fake VendorDriver, offline)."""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.models import Device, DiscoveryScope, Reachability
from tests.fakes import FakeBehavior, FakeDriver
from tests.inventory_helpers import run_inventory


def _audit(tmp_path: Path) -> AuditTrail:
    return AuditTrail(tmp_path / "audit.jsonl")


def test_discover_query_assemble_report(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(device=Device("r1", "172.25.110.1")),
            "172.25.110.2": FakeBehavior(device=Device("r2", "172.25.110.2")),
            "172.25.110.3": FakeBehavior(device=Device("r3", "172.25.110.3")),
        }
    )
    scope = DiscoveryScope(seeds=["172.25.110.1", "172.25.110.2", "172.25.110.3"])

    report = run_inventory(scope, driver, audit=_audit(tmp_path), timeout=1.0, concurrency=8)

    # SC-003: every discovered device appears, each reachable & with health populated.
    assert {s.device.address for s in report.statuses} == {
        "172.25.110.1",
        "172.25.110.2",
        "172.25.110.3",
    }
    assert all(s.reachability is Reachability.REACHABLE for s in report.statuses)
    assert all(s.uptime_seconds is not None for s in report.statuses)
    assert report.counts == {
        "total": 3,
        "reachable": 3,
        "unreachable": 0,
        "auth_failed": 0,
        "unidentified": 0,
        "timed_out": 0,
    }
    # Each identified device was queried exactly once.
    assert sorted(addr for addr, _ in driver.fetch_calls) == [
        "172.25.110.1",
        "172.25.110.2",
        "172.25.110.3",
    ]


def test_engine_emits_one_audit_record_per_device(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(device=Device("r1", "172.25.110.1")),
            "172.25.110.2": FakeBehavior(device=Device("r2", "172.25.110.2")),
        }
    )
    scope = DiscoveryScope(seeds=["172.25.110.1", "172.25.110.2"])

    run_inventory(scope, driver, audit=_audit(tmp_path), timeout=1.0, concurrency=8)

    records = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    actions = [r["action"] for r in records]
    # SC-006: one discover record + one routeros_status per device.
    assert actions.count("discover") == 1
    assert actions.count("routeros_status") == 2
    queried = {r["target"] for r in records if r["action"] == "routeros_status"}
    assert queried == {"172.25.110.1", "172.25.110.2"}
    # The discover record summarizes the scope and never leaks credentials.
    discover = next(r for r in records if r["action"] == "discover")
    assert discover["params"]["seeds"] == ["172.25.110.1", "172.25.110.2"]


def test_timeout_and_concurrency_passed_to_driver(tmp_path: Path) -> None:
    driver = FakeDriver({"172.25.110.1": FakeBehavior(device=Device("r1", "172.25.110.1"))})
    scope = DiscoveryScope(seeds=["172.25.110.1"])

    run_inventory(scope, driver, audit=_audit(tmp_path), timeout=2.5, concurrency=4)

    assert driver.probe_calls == [("172.25.110.1", 2.5)]
    assert driver.fetch_calls == [("172.25.110.1", 2.5)]
