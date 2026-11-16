"""The pre-rendered router.json snapshot (feature 136, T011)."""

from __future__ import annotations

from datetime import UTC, datetime

from obi.core.demo.routerview import parse_export, router_view
from obi.core.demo.routes import UNAVAILABLE_MESSAGE

NOW = datetime(2025, 6, 5, 22, 0, tzinfo=UTC)
EXPORT = """\
# model = CCR2004
/interface bridge add comment="aspen lan" name=bridge
/interface ethernet set [ find default-name=sfp-sfpplus1 ] comment=core-gw-1 name=sfp1-core
/interface ethernet set [ find default-name=ether2 ] disabled=yes
/interface vlan add interface=bridge mtu=1500 name=vlan101-manage vlan-id=101
/interface list member add interface=bridge list=LAN
/interface bridge port add bridge=bridge interface=ether2
/interface wireguard peers add interface=wg0 public-key=<redacted-by-obi>
/ip address add address=172.18.84.11/24 comment=manage interface=vlan101-manage network=172.18.84.0
/ip address add address=172.18.84.1 comment="manage vrrp" interface=vrrp101 network=172.18.84.1
"""
RECORD = {"identity": "core-gw-1", "address": "172.18.84.11", "model": "CCR2004-16G-2S+",
          "version": "7.16.2", "uptime_seconds": 86400, "cpu_load_percent": 4.0}  # fmt: skip


def test_parse_interfaces_and_addresses() -> None:
    interfaces, addresses = parse_export(EXPORT)
    names = {i.name: i for i in interfaces}
    assert set(names) == {"bridge", "sfp1-core", "ether2", "vlan101-manage"}
    assert names["ether2"].disabled is True and names["sfp1-core"].type == "ether"
    assert names["bridge"].comment == "aspen lan" and names["vlan101-manage"].mtu == 1500
    assert [a.address for a in addresses] == ["172.18.84.11/24", "172.18.84.1"]
    assert addresses[1].comment == "manage vrrp"


def test_live_success_shape_marked_snapshot() -> None:
    payload = router_view(RECORD, EXPORT, read_at=NOW)
    assert payload["store"] == "ok" and payload["snapshot"] is True
    assert payload["read"]["outcome"] == "ok" and payload["read"]["read_at"] == NOW.isoformat()
    assert payload["device"]["identity"] == "core-gw-1"
    assert {"interfaces", "addresses", "truncated", "hint"} <= set(payload)
    assert payload["interfaces"][0].keys() == {"name", "type", "running", "disabled", "mac",
                                               "comment", "mtu"}  # fmt: skip


def test_no_export_is_the_demo_unavailable_shape() -> None:
    payload = router_view(RECORD, None, read_at=NOW)
    assert payload["store"] == "absent" and payload["read"]["outcome"] == "unknown_device"
    assert payload["hint"] == UNAVAILABLE_MESSAGE and payload["interfaces"] == []
