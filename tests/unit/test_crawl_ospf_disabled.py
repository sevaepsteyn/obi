"""T014/T016: default-off, zero-impact opt-in — flag off issues zero OSPF reads and behaves like
002; flag on with a non-OspfCapable driver degrades to 002 (feature 004, US2)."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import crawl
from obi.core.drivers.base import ProbeResult
from obi.core.models import Device, DeviceStatus, DiscoveryScope, NeighborLink, OspfNeighbor
from tests.fakes import FakeBehavior, FakeDriver

SCOPE = DiscoveryScope(seeds=["172.25.110.1"], ranges=["172.25.110.0/24"])


def _behaviors() -> dict[str, FakeBehavior]:
    # A link-local peer (would resolve to 172.25.110.9 if OSPF resolution were active) plus a normal
    # in-scope Mikrotik neighbor that 002 already discovers.
    return {
        "172.25.110.1": FakeBehavior(
            neighbors=[
                NeighborLink(address="169.254.0.2", platform="MikroTik"),
                NeighborLink(address="172.25.110.2", platform="MikroTik"),
            ],
            ospf_neighbors=[OspfNeighbor("169.254.0.2", "172.25.110.9")],
        ),
        "172.25.110.2": FakeBehavior(neighbors=[]),
        "172.25.110.9": FakeBehavior(neighbors=[]),
    }


def test_flag_off_issues_zero_ospf_reads_and_matches_002(tmp_path: Path) -> None:
    driver = FakeDriver(_behaviors())
    inv = crawl(
        SCOPE,
        driver,
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        timeout=1.0,
        budget=10.0,
        resolve_link_local_via_ospf=False,
    )
    assert driver.ospf_calls == []  # zero OSPF reads (SC-002)
    # 002 behavior: the normal neighbor is found; the link-local peer is NOT resolved/discovered.
    assert {e.address for e in inv.entries} == {"172.25.110.1", "172.25.110.2"}


def test_flag_absent_defaults_to_off(tmp_path: Path) -> None:
    driver = FakeDriver(_behaviors())
    inv = crawl(SCOPE, driver, audit=AuditTrail(tmp_path / "audit.jsonl"), timeout=1.0, budget=10.0)
    assert driver.ospf_calls == []
    assert {e.address for e in inv.entries} == {"172.25.110.1", "172.25.110.2"}


class _NeighborOnlyDriver:
    """A VendorDriver that is deliberately NOT OspfCapable (no fetch_ospf_neighbors)."""

    name = "neighbor-only"

    def __init__(self, inner: FakeDriver) -> None:
        self._inner = inner

    def probe(self, address: str, *, timeout: float) -> ProbeResult:
        return self._inner.probe(address, timeout=timeout)

    def fetch_status(self, device: Device, *, timeout: float) -> DeviceStatus:
        return self._inner.fetch_status(device, timeout=timeout)

    def fetch_neighbors(self, device: Device, *, timeout: float) -> list[NeighborLink]:
        return self._inner.fetch_neighbors(device, timeout=timeout)


def test_flag_on_but_driver_not_capable_degrades_to_002(tmp_path: Path) -> None:
    driver = _NeighborOnlyDriver(FakeDriver(_behaviors()))
    inv = crawl(
        SCOPE,
        driver,
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        timeout=1.0,
        budget=10.0,
        resolve_link_local_via_ospf=True,
    )
    # No OSPF capability ⇒ link-local peer is not resolved; only 002's neighbor discovery applies.
    assert {e.address for e in inv.entries} == {"172.25.110.1", "172.25.110.2"}
