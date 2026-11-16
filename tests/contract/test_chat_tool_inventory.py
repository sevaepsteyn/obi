"""T017 (US1): inventory_list + inventory_summary — filters, staleness, empty-inventory."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from obi.core.toolset import ToolContext, build_dispatch, build_tools
from tests.fakes import FakeDriver, chat_config, save_inventory


def _ctx(tmp_path: Path) -> ToolContext:
    config = chat_config(tmp_path)
    return ToolContext(
        config=config, driver=FakeDriver({}), audit=AuditTrail(config.audit_path), question="q"
    )


def test_query_inventory_lists_and_filters(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    save_inventory(
        ctx.config.inventory_path,
        [
            InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED),
            InventoryEntry("172.25.110.2", "ap", DiscoveryState.UNREACHABLE),
        ],
    )
    dispatch = build_dispatch(ctx, build_tools())

    listed = dispatch["inventory_list"]({"discovery_state": "any"})
    assert "gw" in listed and "ap" in listed and "last refreshed" in listed

    only_unreachable = dispatch["inventory_list"]({"discovery_state": "unreachable"})
    assert "ap" in only_unreachable and "gw" not in only_unreachable


def test_query_inventory_empty_directs_to_discover(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)  # no inventory file
    out = build_dispatch(ctx, build_tools())["inventory_list"]({})
    assert "obi routeros discover" in out


def test_network_summary_counts(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    save_inventory(
        ctx.config.inventory_path,
        [
            InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED),
            InventoryEntry("172.25.110.2", "ap", DiscoveryState.UNREACHABLE),
        ],
    )
    out = build_dispatch(ctx, build_tools())["inventory_summary"]({})
    assert "2 device(s) total" in out
    assert "identified=1" in out and "unreachable=1" in out
