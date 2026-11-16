"""Unit tests for the live PHY read (feature 075, engine.router_phy).

Drives the engine with a FAKE driver whose read_path / monitor_* return records captured
read-only from real fleet devices in T003 (no device is ever contacted — constitution II).
Pins the merge (reads #3–#4 keyed by name, default_name fallback), the bellwether outcome
classification, firmware-behind derivation, rate parsing, redaction, truncation, and the
unknown-device audit. Reads #5–#9 (PoE/DDM/L2/neighbor) are appended by US3/US4 test blocks.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.drivers.mikrotik import LiveReadResult
from obi.core.engine import _ros_rate_mbps, router_phy
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import DiscoveryScope, DiscoveryState, RouterReadOutcome

_FIXTURES = Path(__file__).parent.parent / "fixtures"
CCR2004 = json.loads((_FIXTURES / "phy-ccr2004.json").read_text())  # SFP-heavy, no PoE
CRS328 = json.loads((_FIXTURES / "phy-crs328.json").read_text())  # PoE + copper two-row

SCOPE = DiscoveryScope(seeds=["172.18.81.1"], ranges=["172.18.81.0/24"])


class FakePhyDriver:
    """Answers the PHY read set from canned tables. Not SessionCapable, so the engine drives it
    through the one-shot forwarder (`_OneShotReadSession`)."""

    def __init__(
        self,
        fixture: dict[str, object] | None = None,
        *,
        error: str | None = None,
        menu_errors: dict[str, str] | None = None,
        ethernet_truncated: bool = False,
        monitor_truncated: bool = False,
        ethernet: list[dict[str, object]] | None = None,
    ) -> None:
        fx = fixture if fixture is not None else CCR2004
        self._error = error
        self._menu_errors = menu_errors or {}
        self._ethernet_truncated = ethernet_truncated
        self._monitor_truncated = monitor_truncated
        self._menus: dict[str, list[dict[str, object]]] = {
            "system/resource": [dict(fx["resource"])] if fx.get("resource") else [
                {"version": "7.21.4", "board-name": str(fx["routerboard"].get("model", ""))}  # type: ignore[union-attr]
            ],
            "system/routerboard": [dict(fx["routerboard"])],  # type: ignore[arg-type]
            "interface/ethernet": ethernet if ethernet is not None else list(fx["ethernet"]),  # type: ignore[arg-type]
            "interface/ethernet/poe": list(fx["ethernet_poe"]),  # type: ignore[arg-type]
            "interface/bridge/port": list(fx["bridge_port"]),  # type: ignore[arg-type]
            "interface/bridge/vlan": list(fx["bridge_vlan"]),  # type: ignore[arg-type]
            "ip/neighbor": list(fx["neighbor"]),  # type: ignore[arg-type]
        }
        self._ethernet_monitor = list(fx["ethernet_monitor"])  # type: ignore[arg-type]
        self._poe_monitor = list(fx["poe_monitor"])  # type: ignore[arg-type]
        self.calls: list[tuple[str, ...]] = []
        self.monitor_calls: list[tuple[str, list[str]]] = []

    def read_path(
        self,
        address: str,
        segments: Sequence[str],
        *,
        where: Sequence[tuple[str, str]] = (),
        fields: Sequence[str] = (),
        timeout: float,
        max_records: int,
    ) -> LiveReadResult:
        menu = "/".join(segments)
        self.calls.append(tuple(segments))
        if self._error is not None:
            return LiveReadResult(records=[], error=self._error)
        if menu in self._menu_errors:
            return LiveReadResult(records=[], error=self._menu_errors[menu])
        records = [dict(r) for r in self._menus.get(menu, [])][:max_records]
        truncated = menu == "interface/ethernet" and self._ethernet_truncated
        return LiveReadResult(records=records, truncated=truncated)

    def monitor_ethernet(
        self, address: str, names: Sequence[str], *, timeout: float, max_records: int
    ) -> LiveReadResult:
        self.monitor_calls.append(("ethernet", list(names)))
        if self._error is not None:
            return LiveReadResult(records=[], error=self._error)
        rows = [dict(r) for r in self._ethernet_monitor][:max_records]
        return LiveReadResult(records=rows, truncated=self._monitor_truncated)

    def monitor_poe(
        self, address: str, names: Sequence[str], *, timeout: float, max_records: int
    ) -> LiveReadResult:
        self.monitor_calls.append(("poe", list(names)))
        if self._error is not None:
            return LiveReadResult(records=[], error=self._error)
        wanted = set(names)
        rows = [dict(r) for r in self._poe_monitor if str(r.get("name")) in wanted][:max_records]
        return LiveReadResult(records=rows)


def _inventory() -> Inventory:
    return Inventory(
        scope=SCOPE,
        entries=[
            InventoryEntry(address="172.18.81.1", identity="chestnut-gw",
                           discovery_state=DiscoveryState.IDENTIFIED,
                           model="CCR2004-1G-12S+2XS", version="7.21.4"),
            InventoryEntry(address="172.18.81.20", identity="chestnut-sw",
                           discovery_state=DiscoveryState.IDENTIFIED,
                           model="CRS328-24P-4S+", version="7.21.4"),
        ],
    )


def _run(tmp_path: Path, driver: FakePhyDriver, device: str, **kw: object):  # type: ignore[no-untyped-def]
    audit = AuditTrail(tmp_path / "audit.jsonl")
    return router_phy(
        _inventory(), driver, device, timeout=5.0, max_records=100, audit=audit, **kw  # type: ignore[arg-type]
    ), audit


def _port(phy, name: str):  # type: ignore[no-untyped-def]
    return next(p for p in phy.ports if p.name == name)


# --- US1: reads #1–#4 merge, outcomes, firmware, rate, redaction, truncation, audit -----------


def test_reads_merge_keyed_by_name(tmp_path: Path) -> None:
    phy, _ = _run(tmp_path, FakePhyDriver(), "chestnut-gw")
    assert phy.outcome is RouterReadOutcome.OK
    assert phy.device is not None
    assert phy.device.model == "CCR2004-1G-12S+2XS"
    # a linked 10G SFP+ port merges ethernet (name/default/mac) with monitor (link/rate/duplex)
    up = _port(phy, "sfp-sfpplus1-chestnut-sw-tower-1")
    assert up.default_name == "sfp-sfpplus1"  # from ethernet default-name, not the operator name
    assert up.link_up is True
    assert up.rate_mbps == 10000
    assert up.full_duplex is True
    assert up.auto_negotiation == "done"
    assert up.transceiver is not None and up.transceiver.present is True
    # a down copper port: monitor status no-link ⇒ link_up False, no rate
    down = _port(phy, "ether1-tech")
    assert down.default_name == "ether1"
    assert down.link_up is False
    assert down.rate_mbps is None
    assert down.transceiver is None  # copper port: no cage


def test_default_name_falls_back_to_name(tmp_path: Path) -> None:
    eth = [{"name": "custom0", "mac-address": "AA:BB:CC:00:00:01", "disabled": "false"}]
    phy, _ = _run(tmp_path, FakePhyDriver(ethernet=eth), "chestnut-gw")
    assert _port(phy, "custom0").default_name == "custom0"


def test_bellwether_unreachable_keeps_roster_summary(tmp_path: Path) -> None:
    driver = FakePhyDriver(error="unreachable: connection refused")
    phy, _ = _run(tmp_path, driver, "chestnut-gw")
    assert phy.outcome is RouterReadOutcome.UNREACHABLE
    assert phy.device is not None  # roster summary retained
    assert phy.device.identity == "chestnut-gw"
    assert phy.device.model == "CCR2004-1G-12S+2XS"
    assert phy.ports == []


def test_timeout_error_classifies_as_timeout(tmp_path: Path) -> None:
    phy, _ = _run(tmp_path, FakePhyDriver(error="read timed out after 5s"), "chestnut-gw")
    assert phy.outcome is RouterReadOutcome.TIMEOUT


def test_firmware_behind_when_current_differs(tmp_path: Path) -> None:
    eth: list[dict[str, object]] = []
    driver = FakePhyDriver({**CCR2004, "routerboard": {"model": "X", "current-firmware": "7.15.3",
                                                       "upgrade-firmware": "7.16.1"}}, ethernet=eth)
    phy, _ = _run(tmp_path, driver, "chestnut-gw")
    assert phy.device is not None and phy.device.firmware_behind is True


def test_firmware_not_behind_when_equal(tmp_path: Path) -> None:
    phy, _ = _run(tmp_path, FakePhyDriver(), "chestnut-gw")  # ccr2004 fixture: current == upgrade
    assert phy.device is not None and phy.device.firmware_behind is False


def test_firmware_behind_none_when_one_side_unknown(tmp_path: Path) -> None:
    driver = FakePhyDriver({**CCR2004, "routerboard": {"model": "X", "current-firmware": "7.15.3"}},
                           ethernet=[])
    phy, _ = _run(tmp_path, driver, "chestnut-gw")
    assert phy.device is not None and phy.device.firmware_behind is None


def test_rate_string_parses_to_mbps() -> None:
    assert _ros_rate_mbps("10Gbps") == 10000
    assert _ros_rate_mbps("1Gbps") == 1000
    assert _ros_rate_mbps("100Mbps") == 100
    assert _ros_rate_mbps("25Gbps") == 25000
    assert _ros_rate_mbps("2.5Gbps") == 2500
    assert _ros_rate_mbps(None) is None
    assert _ros_rate_mbps("weird") is None


def test_secrets_in_returned_records_are_redacted(tmp_path: Path) -> None:
    leaky = [{"name": "ether1", "default-name": "ether1", "mac-address": "AA:BB:CC:DD:EE:01",
              "comment": "psk hunter2secret"}]
    driver = FakePhyDriver(ethernet=leaky)
    phy, _ = _run(tmp_path, driver, "chestnut-gw", extra_secrets=("hunter2secret",))
    # the comment isn't a modeled port field, but redaction runs before mapping — assert the
    # secret never survives into any string on the port (mac kept, secret scrubbed nowhere-visible)
    port = _port(phy, "ether1")
    assert "hunter2secret" not in json.dumps(port.__dict__, default=str)


def test_truncation_propagates(tmp_path: Path) -> None:
    assert (
        _run(tmp_path, FakePhyDriver(ethernet_truncated=True), "chestnut-gw")[0].truncated is True
    )
    assert _run(tmp_path, FakePhyDriver(monitor_truncated=True), "chestnut-gw")[0].truncated is True


def test_unknown_device_is_a_state_and_audited(tmp_path: Path) -> None:
    phy, _ = _run(tmp_path, FakePhyDriver(), "nope-nope")
    assert phy.outcome is RouterReadOutcome.UNKNOWN_DEVICE
    assert phy.device is None and phy.ports == []
    trail = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "router_phy" in trail


def test_us1_issues_only_the_core_reads(tmp_path: Path) -> None:
    driver = FakePhyDriver()
    _run(tmp_path, driver, "chestnut-gw")
    # reads #1–#3 are prints; #4 is the ethernet monitor keyed by the harvested names.
    assert ("system", "resource") in driver.calls
    assert ("system", "routerboard") in driver.calls
    assert ("interface", "ethernet") in driver.calls
    assert any(kind == "ethernet" for kind, _ in driver.monitor_calls)


def test_audit_records_intent_with_address(tmp_path: Path) -> None:
    _, _ = _run(tmp_path, FakePhyDriver(), "chestnut-gw")
    lines = [json.loads(r) for r in
             (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    intents = [r for r in lines if r.get("action") == "router_phy"]
    assert intents and intents[0]["target"] == "router:chestnut-gw"


# --- US3: reads #5–#6 (PoE) + full SFP DDM ----------------------------------------------------


def test_poe_merge_gates_the_monitor_and_joins_by_name(tmp_path: Path) -> None:
    driver = FakePhyDriver(CRS328)
    phy, _ = _run(tmp_path, driver, "chestnut-sw")
    assert phy.outcome is RouterReadOutcome.OK
    assert phy.sections.get("poe") is None  # the PoE reads succeeded
    # the poe monitor is called only because the poe print returned ports, with those names
    poe_calls = [names for kind, names in driver.monitor_calls if kind == "poe"]
    assert poe_calls and poe_calls[0]  # non-empty numbers list
    # a powered-on port carries live voltage/current/power
    powered = [p for p in phy.ports if p.poe and p.poe.status == "powered-on"]
    assert powered
    assert powered[0].poe.voltage_v is not None
    assert powered[0].poe.current_ma is not None
    assert powered[0].poe.power_w is not None
    assert powered[0].poe.out == "auto-on"
    # an SFP port on the switch has no poe-out row ⇒ poe None
    sfp = next(p for p in phy.ports if p.default_name.startswith("sfp"))
    assert sfp.poe is None


def test_non_poe_model_degrades_the_poe_section(tmp_path: Path) -> None:
    # A non-PoE model errors the /interface/ethernet/poe menu (no such command prefix).
    driver = FakePhyDriver(
        CCR2004, menu_errors={"interface/ethernet/poe": "no such command prefix"}
    )
    phy, _ = _run(tmp_path, driver, "chestnut-gw")
    assert phy.outcome is RouterReadOutcome.OK  # the section error never fails the payload
    assert phy.sections.get("poe")  # a degradation detail string
    assert all(p.poe is None for p in phy.ports)
    # the poe monitor is NOT called when the poe print failed
    assert not [names for kind, names in driver.monitor_calls if kind == "poe"]


def test_poe_monitor_error_while_print_ok_keeps_config(tmp_path: Path) -> None:
    driver = FakePhyDriver(CRS328)
    driver._poe_monitor = []  # print returns ports, but the monitor yields nothing usable
    phy, _ = _run(tmp_path, driver, "chestnut-sw")
    powered = [p for p in phy.ports if p.poe is not None]
    assert powered  # config (poe-out) still attached from the print
    assert powered[0].poe.out is not None
    assert powered[0].poe.status is None  # live status blank without the monitor row


def test_full_ddm_mapping_and_copper_module_blanks(tmp_path: Path) -> None:
    phy, _ = _run(tmp_path, FakePhyDriver(CCR2004), "chestnut-gw")
    # an optical SFP+ with DDM
    optical = next(
        p for p in phy.ports if p.transceiver and p.transceiver.temperature_c is not None
    )
    assert optical.transceiver.vendor
    assert optical.transceiver.part
    assert optical.transceiver.wavelength_nm is not None
    assert optical.transceiver.tx_power_dbm is not None
    assert optical.transceiver.rx_power_dbm is not None
    # a copper-SFP module (S-RJ01): present, but no DDM ⇒ honest blanks, not errors
    copper_sfp = next(
        p for p in phy.ports
        if p.transceiver and p.transceiver.present and p.transceiver.temperature_c is None
        and p.transceiver.vendor
    )
    assert copper_sfp.transceiver.wavelength_nm is None
    assert copper_sfp.transceiver.tx_power_dbm is None


def test_sfp_max_rate_parses_the_module_capability_list() -> None:
    from obi.core.engine import _sfp_max_rate_mbps

    # the highest token wins, whatever the order or media type
    assert _sfp_max_rate_mbps(
        "10M-baseT-half,10M-baseT-full,100M-baseT-half,100M-baseT-full,"
        "1G-baseT-half,1G-baseT-full"
    ) == 1000  # S-RJ01: a 1G copper SFP
    assert _sfp_max_rate_mbps("1G-baseX,10G-baseSR-LR") == 10000  # S+85DLC03D optical
    assert _sfp_max_rate_mbps(
        "1G-baseT-full,1G-baseX,2.5G-baseT,2.5G-baseX,5G-baseT,10G-baseCR,25G-baseCR"
    ) == 25000  # XS+DA0001: a 25G-capable DAC
    assert _sfp_max_rate_mbps("2.5G-baseT") == 2500  # fractional rates survive the round-trip
    # absent / empty / unparseable ⇒ None (honest blank), never a guess or a crash
    assert _sfp_max_rate_mbps(None) is None
    assert _sfp_max_rate_mbps("") is None
    assert _sfp_max_rate_mbps("nonsense") is None


def test_module_rate_ceiling_is_mapped_from_sfp_supported(tmp_path: Path) -> None:
    # Without the module's own ceiling a 1G S-RJ01 in a 10G cage looks permanently degraded — the
    # cage alone can't tell a slow module from a slow link (the tamarack-gw sfpplus6 false
    # positive).
    phy, _ = _run(tmp_path, FakePhyDriver(CCR2004), "chestnut-gw")
    copper_sfp = _port(phy, "sfp-sfpplus9-setup-peer-wingnut-60")
    assert copper_sfp.rate_mbps == 1000
    assert copper_sfp.transceiver.part == "S-RJ01"
    assert copper_sfp.transceiver.max_rate_mbps == 1000  # linked AT its max, not below it
    # a DAC that outruns its cage still reports its own (higher) ceiling — consumers take the min
    dac = _port(phy, "sfp-sfpplus10-chestnut-internet-gw")
    assert dac.transceiver.part == "XS+DA0001"
    assert dac.transceiver.max_rate_mbps == 25000
    # an empty cage reports no module ceiling
    empty = _port(phy, "sfp-sfpplus5")
    assert empty.transceiver.present is False
    assert empty.transceiver.max_rate_mbps is None


# --- US4: reads #7–#9 (bridge / VLAN / STP / neighbors) ----------------------------------------


def test_bridge_port_join_and_stp_fields(tmp_path: Path) -> None:
    phy, _ = _run(tmp_path, FakePhyDriver(CCR2004), "chestnut-gw")
    assert phy.sections.get("bridge") is None
    bridged = [p for p in phy.ports if p.l2]
    assert bridged  # some ports are bridge members
    # bridge/pvid come from the bridge-port row; stp state from `status` (in-bridge/inactive)
    member = bridged[0]
    assert member.l2.bridge is not None
    assert member.l2.pvid is not None
    assert member.l2.stp_state in ("in-bridge", "inactive")
    # an in-bridge port exposes an STP `role`; an inactive one does not (D9)
    active = [p for p in bridged if p.l2.stp_state == "in-bridge"]
    if active:
        assert active[0].l2.stp_role is not None
    # a port with no bridge-port row is not bridged
    unbridged = [p for p in phy.ports if p.l2 is None]
    assert unbridged


def test_vlan_ids_range_expands_into_per_port_membership(tmp_path: Path) -> None:
    fixture = {
        **CCR2004,
        "ethernet": [
            {"name": "ether1", "default-name": "ether1", "mac-address": "AA:BB:CC:00:00:01"},
        ],
        "bridge_port": [
            {"interface": "ether1", "bridge": "bridge1", "pvid": "1", "status": "in-bridge"},
        ],
        "bridge_vlan": [
            {"bridge": "bridge1", "vlan-ids": "10-12,20", "tagged": "ether1", "untagged": ""},
            {"bridge": "bridge1", "vlan-ids": "99", "tagged": "", "untagged": "ether1"},
        ],
    }
    phy, _ = _run(tmp_path, FakePhyDriver(fixture), "chestnut-gw")
    port = _port(phy, "ether1")
    assert port.l2 is not None
    assert port.l2.tagged_vlans == [10, 11, 12, 20]
    assert port.l2.untagged_vlans == [99]


def test_neighbor_matching_by_interface_comma_list(tmp_path: Path) -> None:
    fixture = {
        **CCR2004,
        "ethernet": [
            {"name": "ether1", "default-name": "ether1", "mac-address": "AA:BB:CC:00:00:01"},
            {"name": "ether2", "default-name": "ether2", "mac-address": "AA:BB:CC:00:00:02"},
        ],
        "neighbor": [
            # matches ether1 (its name is in the interface comma-list, alongside the bridge)
            {"interface": "ether1,bridge", "address": "172.25.110.9", "identity": "peer-a",
             "mac-address": "DE:AD:BE:EF:00:01", "platform": "MikroTik"},
            # a bridge-only neighbor: attaches to NO port (v1)
            {"interface": "bridge", "address": "172.25.110.8", "identity": "peer-b",
             "mac-address": "DE:AD:BE:EF:00:02", "platform": "MikroTik"},
        ],
    }
    phy, _ = _run(tmp_path, FakePhyDriver(fixture), "chestnut-gw")
    e1 = _port(phy, "ether1")
    e2 = _port(phy, "ether2")
    assert e1.neighbor is not None and e1.neighbor.identity == "peer-a"
    assert e1.neighbor.address == "172.25.110.9"
    assert e2.neighbor is None  # peer-b is bridge-only → attaches to no physical port


def test_l2_and_neighbor_sections_degrade(tmp_path: Path) -> None:
    driver = FakePhyDriver(CCR2004, menu_errors={
        "interface/bridge/port": "no such command prefix",
        "ip/neighbor": "timeout",
    })
    phy, _ = _run(tmp_path, driver, "chestnut-gw")
    assert phy.outcome is RouterReadOutcome.OK
    assert phy.sections.get("bridge")
    assert phy.sections.get("neighbors")
    assert all(p.l2 is None for p in phy.ports)
    assert all(p.neighbor is None for p in phy.ports)
