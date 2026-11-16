"""Scale/performance — ~50 fake devices complete within the time budget.

Validates the performance targets without real hardware: a mix of reachable, unreachable, and
slow devices is driven through the engine (001, SC-002) and the topology crawl (002, SC-008),
each finishing within its budget.
"""

from __future__ import annotations

import time
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import crawl
from obi.core.drivers.base import ProbeOutcome
from obi.core.models import Device, DiscoveryScope, NeighborLink, Reachability
from tests.fakes import FakeBehavior, FakeDriver
from tests.inventory_helpers import run_inventory

BUDGET_SECONDS = 15.0  # SC-002 target for ~50 devices, even with some unreachable
CRAWL_BUDGET_SECONDS = 30.0  # SC-008 target for a ~50-device crawl
PER_DEVICE_TIMEOUT = 0.3


def test_fifty_devices_complete_within_budget(tmp_path: Path) -> None:
    behaviors: dict[str, FakeBehavior] = {}
    seeds: list[str] = []
    for i in range(1, 51):
        address = f"192.0.2.{i}"
        seeds.append(address)
        if i % 5 == 0:
            behaviors[address] = FakeBehavior(outcome=ProbeOutcome.UNREACHABLE)
        elif i % 7 == 0:
            # Slow device that honors the timeout (sleeps up to the timeout, then times out).
            behaviors[address] = FakeBehavior(device=Device(f"r{i}", address), delay=10.0)
        else:
            behaviors[address] = FakeBehavior(device=Device(f"r{i}", address))

    driver = FakeDriver(behaviors)
    scope = DiscoveryScope(seeds=seeds)

    start = time.monotonic()
    report = run_inventory(
        scope,
        driver,
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        timeout=PER_DEVICE_TIMEOUT,
        concurrency=16,
    )
    elapsed = time.monotonic() - start

    # SC-003: every configured device appears.
    assert report.counts["total"] == 50
    assert report.counts["unreachable"] == 10  # every 5th
    assert report.counts["timed_out"] >= 1  # slow ones bounded by the timeout
    assert report.counts["reachable"] >= 1
    # SC-002: bounded, predictable completion.
    assert elapsed < BUDGET_SECONDS, f"run took {elapsed:.2f}s, exceeds {BUDGET_SECONDS}s budget"

    # One audit record per device queried, plus the discover record (SC-006).
    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert sum('"routeros_status"' in line for line in audit_lines) == 50

    # Sanity: a known slow device (i=7) is reported timed_out, not dropped.
    by_address = {s.device.address: s.reachability for s in report.statuses}
    assert by_address["192.0.2.7"] is Reachability.TIMED_OUT


def test_crawl_fifty_devices_completes_within_budget(tmp_path: Path) -> None:
    # A star topology: the seed advertises 49 in-scope Mikrotik neighbors (a few slow,
    # a few unreachable). The crawl must discover all 50 and finish within the budget (SC-008).
    addresses = [f"192.0.2.{i}" for i in range(1, 51)]
    seed = addresses[0]
    behaviors: dict[str, FakeBehavior] = {
        seed: FakeBehavior(
            neighbors=[NeighborLink(address=a, platform="MikroTik") for a in addresses[1:]]
        )
    }
    for i, address in enumerate(addresses[1:], start=2):
        if i % 5 == 0:
            behaviors[address] = FakeBehavior(outcome=ProbeOutcome.UNREACHABLE)
        elif i % 7 == 0:
            behaviors[address] = FakeBehavior(device=Device(f"r{i}", address), probe_delay=0.2)
        else:
            behaviors[address] = FakeBehavior(device=Device(f"r{i}", address))

    driver = FakeDriver(behaviors)
    scope = DiscoveryScope(seeds=[seed], ranges=["192.0.2.0/24"])

    start = time.monotonic()
    inventory = crawl(
        scope,
        driver,
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        timeout=PER_DEVICE_TIMEOUT,
        concurrency=16,
        budget=CRAWL_BUDGET_SECONDS,
    )
    elapsed = time.monotonic() - start

    assert len(inventory.entries) == 50  # SC-003: every discovered device recorded
    assert elapsed < CRAWL_BUDGET_SECONDS, f"crawl took {elapsed:.2f}s"

    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert sum('"probe"' in line for line in audit_lines) == 50  # one per device
    assert sum('"discover"' in line for line in audit_lines) == 1
