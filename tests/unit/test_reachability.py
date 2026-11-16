"""T023: reachability classification — each state is classified and never crashes the run."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.drivers.base import ProbeOutcome, ProbeResult
from obi.core.models import Device, DiscoveryScope, Reachability
from tests.fakes import FakeBehavior, FakeDriver
from tests.inventory_helpers import run_inventory


def _audit(tmp_path: Path) -> AuditTrail:
    return AuditTrail(tmp_path / "audit.jsonl")


def test_each_reachability_state_classified(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(device=Device("ok", "172.25.110.1")),
            "172.25.110.2": FakeBehavior(outcome=ProbeOutcome.UNREACHABLE),
            "172.25.110.3": FakeBehavior(outcome=ProbeOutcome.AUTH_FAILED),
            "172.25.110.4": FakeBehavior(outcome=ProbeOutcome.TIMED_OUT),
            "172.25.110.5": FakeBehavior(outcome=ProbeOutcome.UNIDENTIFIED),
        }
    )
    scope = DiscoveryScope(seeds=[f"172.25.110.{i}" for i in range(1, 6)])

    report = run_inventory(scope, driver, audit=_audit(tmp_path), timeout=1.0, concurrency=8)

    by_address = {s.device.address: s.reachability for s in report.statuses}
    assert by_address == {
        "172.25.110.1": Reachability.REACHABLE,
        "172.25.110.2": Reachability.UNREACHABLE,
        "172.25.110.3": Reachability.AUTH_FAILED,
        "172.25.110.4": Reachability.TIMED_OUT,
        "172.25.110.5": Reachability.UNIDENTIFIED,
    }
    # FR-015: an unidentified host is never silently counted as healthy.
    assert report.counts["reachable"] == 1
    assert report.counts["total"] == 5


class _RaisingFetchDriver:
    """Identifies every address but raises on fetch_status — the engine must not crash."""

    name = "raising"

    def probe(self, address: str, *, timeout: float) -> ProbeResult:
        return ProbeResult(address, ProbeOutcome.IDENTIFIED, device=Device("x", address))

    def fetch_status(self, device: Device, *, timeout: float):  # type: ignore[no-untyped-def]
        raise RuntimeError("driver blew up")


def test_driver_exception_does_not_crash_run(tmp_path: Path) -> None:
    scope = DiscoveryScope(seeds=["172.25.110.1", "172.25.110.2"])

    report = run_inventory(
        scope, _RaisingFetchDriver(), audit=_audit(tmp_path), timeout=1.0, concurrency=4
    )

    assert report.counts["total"] == 2
    assert all(s.reachability is Reachability.UNREACHABLE for s in report.statuses)
    assert all(s.notes for s in report.statuses)  # the failure is explained, not lost
