"""T026 (US3): routeros_status fetches live health for a named device; unreachable → a note."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.inventory import InventoryEntry
from obi.core.models import Device, DeviceStatus, DiscoveryState, Reachability
from obi.core.toolset import ToolContext, build_dispatch, build_tools
from tests.fakes import FakeBehavior, FakeDriver, chat_config, save_inventory


def test_device_status_named_device(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    ctx = ToolContext(
        config=config,
        driver=FakeDriver({"172.25.110.1": FakeBehavior()}),
        audit=AuditTrail(config.audit_path),
        question="q",
    )
    out = build_dispatch(ctx, build_tools())["routeros_status"]({"target": "gw"})
    assert "reachable" in out and "uptime=" in out


def test_device_status_unreachable_noted(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    down = DeviceStatus(Device("gw", "172.25.110.1"), Reachability.UNREACHABLE, notes=["no route"])
    ctx = ToolContext(
        config=config,
        driver=FakeDriver({"172.25.110.1": FakeBehavior(status=down)}),
        audit=AuditTrail(config.audit_path),
        question="q",
    )
    out = build_dispatch(ctx, build_tools())["routeros_status"]({"target": "172.25.110.1"})
    assert "unreachable" in out and "no route" in out
