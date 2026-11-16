"""Unit: the Siklu CLI parsers against the REAL captured output (feature 020).

Fixtures are read-only captures of the tamarack↔basswood pair (Principle II). Covers the
system/inventory/sw/rf/eth/ip parsers, the DDDD:HH:MM:SS uptime + temperature parse, sparse
inventory indices, the ``eth1 operational:down`` port, the empty-SFP slot, and missing-field notes.
"""

from __future__ import annotations

from obi.core.drivers.siklu import (
    build_device,
    parse_eth,
    parse_inventory,
    parse_ip,
    parse_rf,
    parse_sw,
    parse_system,
    parse_uptime,
)
from tests.fakes import load_siklu_fixture

TAMARACK = load_siklu_fixture("tamarack-ptp-basswood-70")
BASSWOOD = load_siklu_fixture("basswood-ptp-tamarack-70")


def test_parse_system_key_values() -> None:
    system = parse_system(TAMARACK["show system"])
    assert system["name"] == "tamarack-ptp-basswood-70"
    assert system["description"] == "EH-8010FX-L"
    assert system["location"] == "tamarack"
    assert system["uptime"] == "0018:00:23:48"
    assert system["temperature"] == "45"
    # A hyphenated, long field name parses cleanly (single-token field, value after first colon).
    assert system["rf-no-traffic-wd-thresh"] == "25"


def test_parse_uptime_dddd_hh_mm_ss() -> None:
    assert parse_uptime("0018:00:23:48") == 18 * 86400 + 0 * 3600 + 23 * 60 + 48
    assert parse_uptime("0000:00:00:00") == 0
    assert parse_uptime(None) is None
    assert parse_uptime("not-an-uptime") is None
    assert parse_uptime("1:2:3") is None  # too few fields


def test_parse_inventory_sparse_indices_and_chassis() -> None:
    inv = parse_inventory(TAMARACK["show inventory"])
    # Indices are sparse: the live unit skips 7/8/11/12/15.
    assert 1 in inv and 16 in inv
    assert 7 not in inv and 11 not in inv
    chassis = inv[1]
    assert chassis["serial"] == "2B058C1831BA"
    assert chassis["model-name"] == "EH-8010FX-ODUL-2C1P-EX-D"
    assert chassis["mfg-name"] == "Siklu"
    assert chassis["sw-rev"] == "10.8.1-19356-c26e1a3193"


def test_parse_sw_running_bank() -> None:
    # tamarack runs bank 2 (10.8.1); bank 1 (10.8.2) is present but not running.
    assert parse_sw(TAMARACK["show sw"]) == "10.8.1-19356-c26e1a3193"
    # basswood: both banks 10.8.1, bank 2 running.
    assert parse_sw(BASSWOOD["show sw"]) == "10.8.1-19356-c26e1a3193"
    assert parse_sw("garbage with no table") is None


def test_parse_rf_metrics_and_asymmetry() -> None:
    sh = parse_rf(TAMARACK["show rf"])
    assert sh.operational is True
    assert sh.rssi_dbm == -44
    assert sh.cinr_db == 27
    assert sh.mode == "adaptive qam128"
    assert sh.air_capacity_mbps == 10000
    assert sh.tx_power_dbm == 14
    assert sh.channel_width_mhz == 2000
    assert sh.tx_frequency_mhz == 74625
    assert sh.lowest_modulation == "qpsk2"

    mt = parse_rf(BASSWOOD["show rf"])
    # The healthy-link per-direction asymmetry: basswood runs qam32 / 5000 while tamarack ran
    # qam128.
    assert mt.operational is True
    assert mt.mode == "adaptive qam32"
    assert mt.air_capacity_mbps == 5000
    assert mt.rssi_dbm == -43


def test_parse_eth_ports_and_eth1_down() -> None:
    ports = {p.name: p for p in parse_eth(TAMARACK["show eth"])}
    assert list(ports) == ["host", "eth0", "eth1", "eth2"]
    # host is the internal switch port: no admin/operational state.
    assert ports["host"].admin_up is None
    assert ports["host"].operational_up is None
    assert ports["host"].tx_throughput == 50410
    # eth1 is admin-up but operationally DOWN (the captured asymmetry).
    assert ports["eth1"].admin_up is True
    assert ports["eth1"].operational_up is False
    assert ports["eth1"].connector_type == "rj45"
    # eth2 is the dual-media port; the SFP slot is empty (all DDM statuses "No SFP").
    assert ports["eth2"].connector_type == "dual-media"
    assert ports["eth2"].sfp_present is False
    assert ports["eth2"].sfp_ddm is None
    assert ports["eth2"].speed_type == "10000fd"  # eth-act-type preferred


def test_parse_eth_sfp_present_when_ddm_real() -> None:
    # Synthesize an eth port with a real (non-"No SFP") DDM reading.
    text = (
        "eth eth2 connector-type : dual-media\n"
        "eth eth2 sfp-ddm-temperature : 34.500000\n"
        "eth eth2 sfp-ddm-temperature-status: Normal\n"
        "eth eth2 sfp-ddm-rx-power : -5.200000\n"
        "eth eth2 sfp-ddm-rx-power-status : Normal\n"
        "eth eth2 admin : up\n"
        "eth eth2 operational : up\n"
    )
    port = parse_eth(text)[0]
    assert port.sfp_present is True
    assert port.sfp_ddm == {"temperature": 34.5, "rx-power": -5.2}


def test_parse_eth_no_ddm_keys_is_none() -> None:
    # A port with no sfp-ddm lines ⇒ sfp_present None (cannot tell), not False.
    port = parse_eth("eth eth0 admin : up\neth eth0 operational : up\n")[0]
    assert port.sfp_present is None
    assert port.sfp_ddm is None


def test_parse_ip_sparse_index() -> None:
    sh = parse_ip(TAMARACK["show ip"])
    # tamarack records its mgmt under index 2; basswood under index 1 — both parse by leading int.
    assert 2 in sh and sh[2]["ip-addr"] == "dhcp 172.18.89.130"
    mt = parse_ip(BASSWOOD["show ip"])
    assert 1 in mt and mt[1]["default-gateway"] == "172.18.83.1"


def test_build_device_identity_serial_model_version() -> None:
    system = parse_system(TAMARACK["show system"])
    inventory = parse_inventory(TAMARACK["show inventory"])
    running = parse_sw(TAMARACK["show sw"])
    device = build_device("172.18.89.130", system, inventory, running)
    assert device.identity == "tamarack-ptp-basswood-70"
    assert device.address == "172.18.89.130"
    assert device.serial == "2B058C1831BA"
    assert device.model == "EH-8010FX-ODUL-2C1P-EX-D"
    assert device.version == "10.8.1-19356-c26e1a3193"


def test_missing_fields_yield_none_not_error() -> None:
    # A sparse system block missing temperature/uptime parses without raising; absent → None.
    system = parse_system("system name : edge-1\n")
    assert system.get("temperature") is None
    assert parse_uptime(system.get("uptime")) is None
    rf = parse_rf("rf operational : up\n")  # only operational present
    assert rf.operational is True
    assert rf.rssi_dbm is None
    assert rf.mode is None
