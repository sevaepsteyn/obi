"""Unit tests for the prefix-preserving IP remap (feature 134, T006)."""

from __future__ import annotations

import ipaddress

import pytest

from obi.core.demo import ProfileError
from obi.core.demo.ipremap import IPV4_LITERAL, IpRemapper, is_allowed_v4

SEED = b"\x01" * 32


def test_private_block_lands_in_target_pool_and_keeps_host_octet() -> None:
    remap = IpRemapper(SEED)
    fake = ipaddress.IPv4Address(remap.map_ip("10.99.5.1"))
    assert fake in ipaddress.IPv4Network("172.16.0.0/12")
    assert fake.packed[3] == 1


def test_same_real_slash24_lands_in_one_fake_slash24() -> None:
    remap = IpRemapper(SEED)
    a = ipaddress.IPv4Address(remap.map_ip("10.99.5.1"))
    b = ipaddress.IPv4Address(remap.map_ip("10.99.5.101"))
    assert a.packed[:3] == b.packed[:3]
    assert b.packed[3] == 101


def test_third_octet_permutation_is_bijective_and_seeded() -> None:
    remap = IpRemapper(SEED)
    thirds = {ipaddress.IPv4Address(remap.map_ip(f"10.99.{i}.1")).packed[2] for i in range(256)}
    assert len(thirds) == 256
    other = IpRemapper(b"\x02" * 32)
    sample = [remap.map_ip(f"10.99.{i}.1") for i in range(8)]
    assert sample != [other.map_ip(f"10.99.{i}.1") for i in range(8)]


def test_site_octet_numbering_does_not_survive() -> None:
    remap = IpRemapper(SEED)
    same = sum(
        ipaddress.IPv4Address(remap.map_ip(f"10.99.{i}.1")).packed[2] == i for i in range(256)
    )
    assert same < 16


def test_subnet_membership_preserved_at_every_prefix_length() -> None:
    remap = IpRemapper(SEED)
    for plen in range(17, 25):
        net = ipaddress.ip_network(f"10.99.4.0/{plen}", strict=False)
        fake_net = ipaddress.ip_network(remap.map_ip(f"{net.network_address}/{plen}"))
        assert fake_net.prefixlen == plen
        for host in ("10.99.4.1", "10.99.5.200", "10.99.7.9"):
            inside = ipaddress.ip_address(host) in net
            fake_host = ipaddress.ip_address(remap.map_ip(host))
            assert (fake_host in fake_net) == inside


def test_networks_stay_canonical_and_interface_addresses_keep_host_bits() -> None:
    remap = IpRemapper(SEED)
    assert remap.map_ip("10.99.0.0/16").endswith(".0.0/16")
    assert remap.map_ip("10.0.0.0/8") == "172.16.0.0/12"
    iface = remap.map_ip("10.99.5.1/24")
    assert iface.endswith(".1/24") and iface.split("/")[0] == remap.map_ip("10.99.5.1")


def test_cidr_prefix_lengths_preserved() -> None:
    remap = IpRemapper(SEED)
    for plen in ("24", "30", "32", "16"):
        out = remap.map_ip(f"10.99.5.1/{plen}")
        assert out.endswith(f"/{plen}")
        assert ipaddress.ip_network(out, strict=False).prefixlen == int(plen)


def test_distinct_real_blocks_get_distinct_fake_blocks() -> None:
    remap = IpRemapper(SEED)
    a = remap.map_ip("10.99.0.1")
    b = remap.map_ip("10.98.0.1")
    assert a.split(".")[:2] != b.split(".")[:2]


def test_public_ips_go_to_documentation_ranges_grouped_by_slash24() -> None:
    remap = IpRemapper(SEED)
    a = ipaddress.IPv4Address(remap.map_ip("8.8.4.4"))
    b = ipaddress.IPv4Address(remap.map_ip("8.8.4.9"))
    assert a in ipaddress.IPv4Network("192.0.2.0/24")
    assert a.packed[:3] == b.packed[:3]
    for i in range(10):
        assert is_allowed_v4(ipaddress.IPv4Address(remap.map_ip(f"9.{i}.0.1")))


def test_unchanged_set_passes_through() -> None:
    remap = IpRemapper(SEED)
    for addr in (
        "169.254.1.4",
        "127.0.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "224.0.0.5",
        "255.255.255.0",
        "0.0.0.255",
    ):
        assert remap.map_ip(addr) == addr


def test_ipv6_global_moves_to_documentation_and_link_local_stays() -> None:
    remap = IpRemapper(SEED)
    out = ipaddress.IPv6Address(remap.map_ip("2600:1700:abcd:1::5"))
    assert out in ipaddress.IPv6Network("2001:db8::/32")
    assert str(out).endswith("1::5")
    assert remap.map_ip("fe80::1") == "fe80::1"


def test_pool_collision_is_fatal() -> None:
    remap = IpRemapper(SEED)
    with pytest.raises(ProfileError):
        remap.check_pools_disjoint(["172.20.0.0/16"])
    remap.check_pools_disjoint(["10.99.0.0/16"])


def test_deterministic_for_same_seed_and_persisted_state_is_stable() -> None:
    a = IpRemapper(SEED)
    b = IpRemapper(SEED)
    addrs = ["10.99.5.1", "10.98.7.3", "8.8.4.4", "2600:1700:abcd:1::5"]
    assert [a.map_ip(x) for x in addrs] == [b.map_ip(x) for x in addrs]
    reloaded = IpRemapper(b"\x09" * 32, a.state())  # a different seed: state still wins
    assert [reloaded.map_ip(x) for x in addrs] == [a.map_ip(x) for x in addrs]


@pytest.mark.parametrize(
    "text",
    [
        "oid 1.3.6.1.4.1.14988.1.1.1",
        "RouterOS 6.49.10.1.2 upgraded",
        "sysObjectID=1.3.6.1.2.1.1",
    ],
)
def test_oids_and_versions_are_not_ip_literals(text: str) -> None:
    assert IPV4_LITERAL.search(text) is None
    assert IpRemapper(SEED).rewrite_text(text) == text


def test_rewrite_text_maps_literals_in_prose_and_keeps_trailing_punctuation() -> None:
    remap = IpRemapper(SEED)
    out = remap.rewrite_text("gw 10.99.5.1. peer 10.99.5.2/30, mac aa:bb:cc:dd:ee:ff at 12:00:00")
    assert "10.99" not in out
    assert out.endswith("mac aa:bb:cc:dd:ee:ff at 12:00:00")
    assert "/30," in out


def test_cgnat_hosts_map_host_by_host_into_reserved_block() -> None:
    remap = IpRemapper(SEED)
    fakes = {remap.map_ip(f"100.{64 + i}.{i}.{i + 1}") for i in range(40)}
    assert len(fakes) == 40
    assert all(ipaddress.IPv4Address(f) in ipaddress.IPv4Network("172.31.0.0/16") for f in fakes)
    assert remap.map_ip("100.64.1.2") == IpRemapper(SEED).map_ip("100.64.1.2")
    remap.check_pools_disjoint(["100.64.1.2/32"])  # real CGNAT no longer collides
    block = remap.map_ip("10.99.0.1")
    assert not block.startswith("172.31.")
