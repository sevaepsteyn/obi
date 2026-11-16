"""T024: partial results + timeout under field conditions (FR-005, FR-006, SC-002)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from obi.core.audit import AuditTrail
from obi.core.drivers.base import ProbeOutcome
from obi.core.models import Device, DiscoveryScope, Reachability
from tests.fakes import FakeBehavior, FakeDriver
from tests.inventory_helpers import run_inventory


def _audit(tmp_path: Path) -> AuditTrail:
    return AuditTrail(tmp_path / "audit.jsonl")


def test_slow_device_timed_out_others_returned_in_budget(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(device=Device("fast1", "172.25.110.1")),
            # honors the timeout: sleeps up to `timeout`, then reports timed_out
            "172.25.110.2": FakeBehavior(device=Device("slow", "172.25.110.2"), delay=5.0),
            "172.25.110.3": FakeBehavior(device=Device("fast2", "172.25.110.3")),
        }
    )
    scope = DiscoveryScope(seeds=["172.25.110.1", "172.25.110.2", "172.25.110.3"])

    start = time.monotonic()
    report = run_inventory(scope, driver, audit=_audit(tmp_path), timeout=0.2, concurrency=8)
    elapsed = time.monotonic() - start

    by_address = {s.device.address: s.reachability for s in report.statuses}
    assert by_address["172.25.110.2"] is Reachability.TIMED_OUT
    assert by_address["172.25.110.1"] is Reachability.REACHABLE
    assert by_address["172.25.110.3"] is Reachability.REACHABLE
    assert elapsed < 2.0, f"run took {elapsed:.2f}s — should be bounded by the timeout"


def test_wedged_device_does_not_block_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Shrink the engine's grace so the test is fast; the wedged device ignores its timeout.
    monkeypatch.setattr("obi.core.engine._TIMEOUT_GRACE", 0.3)
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(device=Device("fast", "172.25.110.1")),
            "172.25.110.2": FakeBehavior(
                device=Device("wedged", "172.25.110.2"), delay=2.0, hang=True
            ),
        }
    )
    scope = DiscoveryScope(seeds=["172.25.110.1", "172.25.110.2"])

    start = time.monotonic()
    report = run_inventory(scope, driver, audit=_audit(tmp_path), timeout=0.1, concurrency=8)
    elapsed = time.monotonic() - start

    by_address = {s.device.address: s.reachability for s in report.statuses}
    assert by_address["172.25.110.2"] is Reachability.TIMED_OUT  # caught by the engine net
    assert by_address["172.25.110.1"] is Reachability.REACHABLE
    assert elapsed < 1.5, f"run took {elapsed:.2f}s — a wedged device must not block the report"


def test_all_unreachable_returns_clean_full_listing(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(outcome=ProbeOutcome.UNREACHABLE),
            "172.25.110.2": FakeBehavior(outcome=ProbeOutcome.TIMED_OUT),
            "172.25.110.3": FakeBehavior(outcome=ProbeOutcome.AUTH_FAILED),
        }
    )
    scope = DiscoveryScope(seeds=["172.25.110.1", "172.25.110.2", "172.25.110.3"])

    report = run_inventory(scope, driver, audit=_audit(tmp_path), timeout=0.5, concurrency=8)

    assert report.counts["total"] == 3
    assert report.counts["reachable"] == 0
    assert {s.reachability for s in report.statuses} == {
        Reachability.UNREACHABLE,
        Reachability.TIMED_OUT,
        Reachability.AUTH_FAILED,
    }
