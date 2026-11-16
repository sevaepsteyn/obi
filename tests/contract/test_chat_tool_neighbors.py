"""T018 (US1): routeros_neighbors reads a device's neighbors live and flags non-Mikrotik ones."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState, NeighborLink
from obi.core.toolset import ToolContext, build_dispatch, build_tools
from tests.fakes import FakeBehavior, FakeDriver, chat_config, save_inventory


def test_neighbors_lists_and_flags_non_mikrotik(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    behaviors = {
        "172.25.110.1": FakeBehavior(
            neighbors=[
                NeighborLink(address="172.25.110.2", identity="rtr2", platform="MikroTik"),
                NeighborLink(address="172.25.110.3", identity="sw", platform="Ubiquiti Networks"),
            ]
        )
    }
    ctx = ToolContext(
        config=config,
        driver=FakeDriver(behaviors),
        audit=AuditTrail(config.audit_path),
        question="q",
    )
    out = build_dispatch(ctx, build_tools())["routeros_neighbors"]({"target": "gw"})

    assert "rtr2" in out and "sw" in out
    assert "out of scope" in out  # the Ubiquiti neighbor is flagged non-Mikrotik


def test_neighbors_unknown_device(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    ctx = ToolContext(
        config=config, driver=FakeDriver({}), audit=AuditTrail(config.audit_path), question="q"
    )
    out = build_dispatch(ctx, build_tools())["routeros_neighbors"]({"target": "nope"})
    assert "No device named 'nope'" in out
