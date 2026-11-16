"""Integration (US2): a whole-fleet question consults BOTH vendors; the tool results fed back to
the model are vendor-labeled so the answer can distinguish them (feature 006)."""

from __future__ import annotations

from pathlib import Path

from obi.chat.session import ask
from obi.core.inventory import InventoryEntry
from obi.core.models import Device, DeviceStatus, DiscoveryState, Reachability
from tests.fakes import (
    FakeBehavior,
    FakeSource,
    chat_config,
    chat_runtime,
    save_inventory,
    text_response,
    tool_response,
    uisp_settings,
    unifi_network_settings,
)


def test_us2_whole_fleet_spans_both_vendors(tmp_path: Path) -> None:
    config = chat_config(tmp_path, uisp=uisp_settings())
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    uisp_down = DeviceStatus(
        Device("valley-ap", "10.9.0.6"), Reachability.UNREACHABLE, notes=["offline"]
    )
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("routeros_status", {"target": "all"}, use_id="t1"),
            tool_response("uisp_status", {}, use_id="t2"),
            text_response("Mikrotik gw is up; UISP valley-ap is down."),
        ],
        behaviors={"172.25.110.1": FakeBehavior()},
        uisp_source=FakeSource([uisp_down]),
    )

    result = ask(runtime, [], "what's down across the whole network?")

    # The model consulted both vendors.
    assert result.tools_used == ["routeros_status", "uisp_status"]

    # Both vendors were read live and audited.
    audit = (tmp_path / "audit.jsonl").read_text()
    assert '"uisp_status"' in audit  # UISP live read
    assert '"routeros_status"' in audit  # Mikrotik live read

    # The UISP tool result handed back to the model is vendor-labeled, so a combined answer can
    # distinguish a UISP device from a Mikrotik one (US2 scenario 3 disambiguation).
    fed_back = str(runtime.client.calls[-1]["messages"])
    assert "UISP valley-ap" in fed_back


def test_whole_fleet_no_longer_reads_unifi(tmp_path: Path) -> None:
    # Feature 105 replaces SC-008 (feature 011): unifi_status is disabled registry-wide, so a
    # whole-fleet question consults Mikrotik + UISP only — even with a configured UniFi console
    # and a live source wired in, the runtime does not OFFER the tool.
    config = chat_config(tmp_path, unifi_network=unifi_network_settings())
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    unifi_down = DeviceStatus(
        Device("uap-shade", "10.40.0.3"), Reachability.UNREACHABLE, notes=["offline"]
    )
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("routeros_status", {"target": "all"}, use_id="t1"),
            text_response("Mikrotik gw is up; UniFi is not read live."),
        ],
        behaviors={"172.25.110.1": FakeBehavior()},
        unifi_network_source=FakeSource([unifi_down]),
    )

    assert "unifi_status" not in {tool.name for tool in runtime.tools}

    result = ask(runtime, [], "what's down across the whole network?")

    assert result.tools_used == ["routeros_status"]
    audit = (tmp_path / "audit.jsonl").read_text()
    assert '"routeros_status"' in audit  # Mikrotik live read, audited
    assert '"unifi_status"' not in audit  # no UniFi read happened, none was audited
