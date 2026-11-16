"""Contract: the PHY console + faceplates actually RENDER end-to-end in node (075).

The other PHY assertions (asset-source string matches, the engine/serializer unit + contract tests)
can't catch a runtime failure in phy.js mount()/renderFaceplate() or a bad faceplate layout. This
suite executes the REAL modules in the DOM-stub harness (tests/web_harness/phy_headless.mjs):

- the faceplates.js invariant lint (port counts, unique ids/cells, poe only on poe models);
- phy.js mounted against a real fixture-derived payload → SVG faceplate with per-port LED classes;
- the generic-layout fallback for an unknown model + the firmware-behind flag;
- honest unreachable / store-absent states (no faceplate, a clear message);
- the router.js tab shell still renders the L3 feed (SC-007 guard).

Synthetic + captured fixtures only; skipped where node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "phy_headless.mjs"
FIXTURES = Path(__file__).parent.parent / "fixtures"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(target: str, data_file: Path | None = None) -> dict:
    args = ["node", str(HARNESS), target, str(STATIC_DIR)]
    if data_file is not None:
        args.append(str(data_file))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _phy_payload(
    fixture_name: str, model: str, identity: str, address: str,
    menu_errors: dict[str, str] | None = None,
) -> dict:
    """Drive the REAL engine + serializer against a captured fixture to produce a phy.json payload
    (no device is contacted — the fixture stands in for one, constitution II)."""

    from obi.core.audit import AuditTrail
    from obi.core.engine import router_phy
    from obi.core.inventory import Inventory, InventoryEntry
    from obi.core.models import DiscoveryScope, DiscoveryState
    from obi.core.serialize import router_phy_to_dict
    from tests.unit.test_router_phy import FakePhyDriver

    fixture = json.loads((FIXTURES / fixture_name).read_text())
    driver = FakePhyDriver(fixture, menu_errors=menu_errors)
    inventory = Inventory(
        scope=DiscoveryScope(seeds=[address]),
        entries=[InventoryEntry(address=address, identity=identity,
                                discovery_state=DiscoveryState.IDENTIFIED,
                                model=model, version="7.21.4")],
    )
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        phy = router_phy(inventory, driver, identity, timeout=5.0, max_records=100,
                         audit=AuditTrail(Path(td) / "audit.jsonl"))
    payload = router_phy_to_dict(phy)
    payload.update({"store": "ok", "hint": None,
                    "metrics": {"store": "absent", "window_seconds": 0, "error_increases": []}})
    return payload


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_faceplate_layouts_pass_the_invariant_lint() -> None:
    out = _run("lint")
    assert out["problems"] == [], out["problems"]
    assert out["models"] == 5


def test_phy_renders_a_faceplate_with_per_port_led_state(tmp_path: Path) -> None:
    payload = _phy_payload("phy-crs328.json", "CRS328-24P-4S+", "chestnut-sw", "172.18.81.20")
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    assert out["faceplate"] is True
    assert out["portCount"] == 28  # 24 PoE copper + 4 SFP+
    # the fixture has a real spread of link states — at least one lit and one non-lit port
    states = set(out["cells"].values())
    assert "phy-lit" in states
    assert states & {"phy-off", "phy-disabled"}
    assert out["cageOccupied"] >= 1  # some SFP+ modules present
    assert out["statusClass"] == "kit-status ok"  # 076: the shared kit status line


def test_phy_port_click_inspects_and_tables_link_to_ports(tmp_path: Path) -> None:
    # US2: clicking a faceplate port pins the inspector + highlights it; the per-port table renders
    # a row per port and a row click repoints the selection; 056 context descriptors are attached.
    payload = _phy_payload("phy-crs328.json", "CRS328-24P-4S+", "chestnut-sw", "172.18.81.20")
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    link = next(s for s in out["sections"] if s["label"] == "per-port link")
    assert link["rows"] == 28  # a table row per port
    assert out["clickedFieldCount"] >= 6  # inspector shows the link fields
    assert out["selectedAfterPortClick"] is not None  # the clicked port is highlighted
    # 056 "ask obi about this" descriptors on both the SVG port and the table row
    assert out["portContextSubject"] and "on chestnut-sw" in out["portContextSubject"]
    assert out["rowContextSubject"] and "on chestnut-sw" in out["rowContextSubject"]
    # a table row click repoints the faceplate selection
    assert out["selectedAfterRowClick"] is not None
    assert out["selectedAfterRowClick"] != out["selectedAfterPortClick"]
    # 076: the inspector is now a dismissible kit panel — the ✕ closes it AND deselects the port
    # (before 075 the in-flow inspector could not be dismissed at all).
    assert out["inspectorOpen"] is True
    assert out["dismissedClosesPanel"] is True
    assert out["dismissedDeselects"] is True


def _section(out: dict, label: str) -> dict:
    return next(s for s in out["sections"] if s["label"] == label)


def test_phy_transceiver_and_poe_tables_render(tmp_path: Path) -> None:
    # US3: a PoE switch shows the SFP DDM table, the PoE-out table with rows, and PoE affordance
    # bars on the faceplate copper ports.
    payload = _phy_payload("phy-crs328.json", "CRS328-24P-4S+", "chestnut-sw", "172.18.81.20")
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    assert out["poeBars"] > 0
    sfp = _section(out, "transceivers (SFP DDM)")
    assert sfp["hidden"] is False and sfp["rows"] >= 1
    poe = _section(out, "PoE-out")
    assert poe["hidden"] is False and poe["rows"] >= 1


def test_phy_structural_no_poe_hides_the_section(tmp_path: Path) -> None:
    # C2: a non-PoE model (layout has no PoE ports) renders NO PoE section, even when the poe read
    # degraded — the SFP DDM table still renders (this model is SFP-heavy).
    payload = _phy_payload("phy-ccr2004.json", "CCR2004-1G-12S+2XS", "chestnut-gw", "172.18.81.1",
                           menu_errors={"interface/ethernet/poe": "no such command prefix"})
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    assert out["poeBars"] == 0
    assert _section(out, "PoE-out")["hidden"] is True
    assert _section(out, "transceivers (SFP DDM)")["hidden"] is False


def test_phy_degraded_poe_on_poe_model_shows_empty_state(tmp_path: Path) -> None:
    # C2: a PoE model whose poe read degraded renders the section with an empty/degraded state + the
    # detail, not nothing.
    payload = _phy_payload("phy-crs328.json", "CRS328-24P-4S+", "chestnut-sw", "172.18.81.20",
                           menu_errors={"interface/ethernet/poe": "timeout reading poe"})
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    poe = _section(out, "PoE-out")
    assert poe["hidden"] is False
    assert poe["degraded"] is True


def test_phy_module_rate_ceiling_beats_the_cage_max(tmp_path: Path) -> None:
    # A port is judged against the LESSER of its cage's max and its module's — end-to-end, from the
    # captured device fields through the engine/serializer into the rendered LED + badge.
    payload = _phy_payload("phy-ccr2004.json", "CCR2004-1G-12S+2XS", "chestnut-gw", "172.18.81.1")
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    # a 1G S-RJ01 linked at 1G sits in a 10G cage: at its own max ⇒ lit and unbadged, not "below
    # max" (the tamarack-gw sfpplus6 false positive this fixture's chestnut-gw twin reproduces)
    assert out["cells"]["sfp-sfpplus9"] == "phy-lit"
    assert "sfp-sfpplus9" not in out["badgedPorts"]
    # the converse guard: a 25G XS+DA0001 DAC linked at 10G is capped by its 10G CAGE, not
    # degraded — judging by the module's max alone would falsely badge every DAC in the fleet
    for dac in ("sfp-sfpplus10", "sfp-sfpplus11", "sfp-sfpplus12"):
        assert out["cells"][dac] == "phy-lit"
        assert dac not in out["badgedPorts"]
    assert out["badgeCount"] == 0  # this capture is a healthy router: nothing to warn about


def _badge_payload(metrics: dict) -> dict:
    def port(name, default, *, rate, full_duplex=True, temp=None, module_max=None):
        transceiver = None
        if temp is not None:
            transceiver = {"present": True, "vendor": "V", "part": "P", "wavelength_nm": 850,
                           "max_rate_mbps": module_max, "temperature_c": temp,
                           "tx_power_dbm": -2, "rx_power_dbm": -3}
        return {"name": name, "default_name": default, "mac": None, "disabled": False,
                "link_up": True, "rate_mbps": rate, "full_duplex": full_duplex,
                "auto_negotiation": "done", "transceiver": transceiver, "poe": None,
                "l2": None, "neighbor": None}

    return {
        "store": "ok",
        "hint": None,
        "device": {
            "identity": "x",
            "address": "172.25.110.9",
            "model": "Unknown-9000",
            "serial": "S",
            "version": "7.1",
            "current_firmware": "7.1",
            "upgrade_firmware": "7.1",
            "firmware_behind": False,
        },
        "read": {"outcome": "ok", "detail": None, "read_at": "2026-07-15T18:00Z"},
        "ports": [
            port("ether1", "ether1", rate=1000, full_duplex=False),  # half-duplex
            port("ether2", "ether2", rate=100),  # below max (1000)
            port("sfp-a", "sfp-sfpplus1", rate=10000, temp=75),  # hot optic (>=70)
            port("ether3", "ether3", rate=1000),  # errors climbing (from metrics)
            port("ether4", "ether4", rate=1000),  # healthy — no badge
        ],
        "sections": {},
        "truncated": False,
        "metrics": metrics,
    }


def test_phy_badges_cover_all_four_rules(tmp_path: Path) -> None:
    payload = _badge_payload(
        {"store": "ok", "window_seconds": 3600,
         "error_increases": [{"ifname": "ether3", "metric": "ifInErrors", "increase": 17}]}
    )
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    assert out["badgeCount"] == 4
    assert set(out["badgedPorts"]) == {"ether1", "ether2", "sfp-sfpplus1", "ether3"}
    assert "ether4" not in out["badgedPorts"]  # the healthy full-rate port never badges


def test_phy_metrics_absent_drops_only_the_error_badge(tmp_path: Path) -> None:
    payload = _badge_payload({"store": "absent", "window_seconds": 0, "error_increases": []})
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]  # nothing breaks with no metrics
    assert out["badgeCount"] == 3  # the errors-climbing badge is silently absent
    assert "ether3" not in out["badgedPorts"]


def test_phy_l2_and_neighbor_tables_render(tmp_path: Path) -> None:
    # US4: a bridged switch shows the bridge/VLAN/STP table and the neighbors table with rows.
    payload = _phy_payload("phy-crs328.json", "CRS328-24P-4S+", "chestnut-sw", "172.18.81.20")
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    l2 = _section(out, "bridge / VLAN / STP")
    assert l2["hidden"] is False and l2["rows"] >= 1
    neighbors = _section(out, "neighbors")
    assert neighbors["hidden"] is False and neighbors["rows"] >= 1


def test_phy_degraded_l2_and_neighbor_sections_show_state(tmp_path: Path) -> None:
    payload = _phy_payload("phy-crs328.json", "CRS328-24P-4S+", "chestnut-sw", "172.18.81.20",
                           menu_errors={"interface/bridge/port": "no such command prefix",
                                        "ip/neighbor": "timeout"})
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    assert _section(out, "bridge / VLAN / STP")["degraded"] is True
    assert _section(out, "neighbors")["degraded"] is True


def test_phy_generic_layout_and_firmware_behind(tmp_path: Path) -> None:
    payload = {
        "store": "ok", "hint": None,
        "device": {"identity": "x", "address": "172.25.110.9", "model": "Weird-Router-3000",
                   "serial": "S", "version": "7.1", "current_firmware": "7.1",
                   "upgrade_firmware": "7.2", "firmware_behind": True},
        "read": {"outcome": "ok", "detail": None, "read_at": "2026-07-15T18:00:00+00:00"},
        "ports": [
            {"name": "e1", "default_name": "ether1", "mac": None, "disabled": False,
             "link_up": True, "rate_mbps": 1000, "full_duplex": True, "auto_negotiation": "done",
             "transceiver": None, "poe": None, "l2": None, "neighbor": None},
            {"name": "s1", "default_name": "sfp-sfpplus1", "mac": None, "disabled": False,
             "link_up": False, "rate_mbps": None, "full_duplex": None, "auto_negotiation": None,
             "transceiver": {"present": False, "vendor": None, "part": None, "wavelength_nm": None,
                             "temperature_c": None, "tx_power_dbm": None, "rx_power_dbm": None},
             "poe": None, "l2": None, "neighbor": None},
        ],
        "sections": {}, "truncated": False,
        "metrics": {"store": "absent", "window_seconds": 0, "error_increases": []},
    }
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    assert out["faceplate"] is True
    assert out["portCount"] == 2  # unknown model ⇒ generic layout from the payload ports
    assert out["cells"]["ether1"] == "phy-lit"
    assert out["fwBehind"] is True


def test_phy_unreachable_shows_state_and_keeps_summary(tmp_path: Path) -> None:
    payload = {
        "store": "ok", "hint": None,
        "device": {"identity": "x", "address": "172.25.110.9", "model": "CCR2004-1G-12S+2XS",
                   "serial": None, "version": None, "current_firmware": None,
                   "upgrade_firmware": None, "firmware_behind": None},
        "read": {"outcome": "unreachable", "detail": "unreachable: connection refused",
                 "read_at": "2026-07-15T18:00:00+00:00"},
        "ports": [], "sections": {}, "truncated": False,
        "metrics": {"store": "absent", "window_seconds": 0, "error_increases": []},
    }
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    assert out["faceplate"] is False
    assert "unreachable" in (out["state"] or "")
    assert out["summaryFacts"]  # roster-known summary retained


def test_phy_absent_shows_hint(tmp_path: Path) -> None:
    payload = {
        "store": "absent", "hint": "RouterOS not configured — add a [routeros] section",
        "device": None,
        "read": {"outcome": "unknown_device", "detail": "x", "read_at": "2026-07-15T18:00Z"},
        "ports": [], "sections": {}, "truncated": False,
        "metrics": {"store": "absent", "window_seconds": 0, "error_increases": []},
    }
    out = _run("phy", _write(tmp_path, "phy.json", payload))
    assert out["mountError"] is None, out["mountError"]
    assert out["faceplate"] is False
    assert "not configured" in (out["state"] or "")


def test_router_tab_shell_still_renders_l3(tmp_path: Path) -> None:
    # SC-007: the L3 pane is the pre-075 window; it must still render its feeds through the tabs.
    router_payload = {
        "store": "ok", "hint": None,
        "device": {"identity": "test-gw", "address": "172.25.110.1", "model": "CCR2004-1G-12S+2XS",
                   "version": "7.21.4", "board_name": "CCR2004", "uptime_seconds": 1000,
                   "cpu_load_percent": 3, "free_memory_bytes": 1000000,
                   "total_memory_bytes": 2000000},
        "read": {"outcome": "ok", "detail": None, "read_at": "2026-07-15T18:00:00+00:00"},
        "interfaces": [
            {"name": "ether1", "type": "ether", "running": True, "disabled": False,
             "mac": "AA:BB:CC:00:00:01", "comment": "", "mtu": 1500},
        ],
        "addresses": [
            {"address": "172.25.110.1/24", "network": "172.25.110.0", "interface": "ether1",
             "dynamic": False, "disabled": False, "comment": ""},
        ],
        "truncated": False,
    }
    out = _run("router", _write(tmp_path, "router.json", router_payload))
    assert out["mountError"] is None, out["mountError"]
    assert out["tabCount"] == 3  # L3 + PHY + HEALTH (096)
    assert out["activeTab"] == "L3"
    assert out["l3Rows"] >= 2  # the interface + the address rendered on the kit feed
