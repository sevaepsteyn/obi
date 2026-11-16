"""Leaks found by the 136 publish check, each pinned (fictional values only).

Before the fix, RouterOS exports leaked:
- WireGuard peers named after people;
- WireGuard public keys;
- SSIDs and security-profile names;
- the operator's domain in a firewall comment;
- a dictionary-word place inside a comment;
- an account-scoped cloud-console UUID;
- proper-noun first names that the system word list made look "ordinary".
"""

from __future__ import annotations

from obi.core.demo.gate import _UUID_HOST_SHAPE, _WG_KEY_SHAPE
from obi.core.demo.harvest import Harvest, harvest_routeros_export
from obi.core.demo.naming import is_dictionary_word, load_dictionary

WORDS = load_dictionary()
KEY = "Zm9vYmFyYmF6cXV1eGNvcmdlZ3JhdWx0Z2FycGx5d2E="
EXPORT = f"""# 2026-08-25 10:00:00 by RouterOS 7.16.2
/interface wireguard peers
add allowed-address=10.99.9.2/32 comment="marlo quartzbook" interface=wg1 \\
    name=marlo-quartzbook public-key="{KEY}"
add allowed-address=0.0.0.0/0 comment=northfield-gw name=northfield-gw public-key="{KEY[::-1]}"
add comment=obi name=obi public-key="{KEY.lower()}"
add name=peer2 public-key="{KEY.upper()}"
/interface wireless security-profiles
add authentication-types=wpa2-psk mode=dynamic-keys name=velvet-otter supplicant-identity=MikroTik
/interface wireless set [ find default-name=wlan1 ] ssid="Velvet Otter"
/ip firewall filter add action=accept chain=input comment="accept quuxhost.test" src-address=8.8.4.9
/system ntp client servers add address=0.us.pool.ntp.org
"""


def _harvest() -> Harvest:
    h = Harvest()
    h.place_words["northfield"] = False
    harvest_routeros_export(EXPORT, h, WORDS)
    return h


def test_wireguard_people_are_persons_devices_and_placeholders_are_not() -> None:
    h = _harvest()
    assert {"marlo quartzbook", "marlo-quartzbook", "marlo"} <= h.persons
    assert not {"obi", "peer2", "northfield-gw"} & h.persons


def test_keys_are_secrets_ssids_and_profiles_are_ssids_domains_harvested() -> None:
    h = _harvest()
    assert KEY in h.secrets and len(h.secrets) == 4
    assert {"Velvet Otter", "velvet-otter"} <= h.ssids
    assert "quuxhost.test" in h.domains
    assert not any("ntp.org" in d for d in h.domains)


def test_proper_nouns_are_not_ordinary_words() -> None:
    # The system list has "Mabel" and "Oswald" capitalised only; "barn" stays ordinary.
    for name in ("mabel", "oswald", "agnes"):
        assert not is_dictionary_word(name, WORDS), name
    assert is_dictionary_word("barn", WORDS) and is_dictionary_word("planet", WORDS)


def test_gate_shapes_catch_keys_and_uuid_hosts_independently_of_the_harvest() -> None:
    assert _WG_KEY_SHAPE.search(f'public-key=\\"{KEY}\\"')
    assert not _WG_KEY_SHAPE.search("public-key=<redacted-by-obi>")
    assert _UUID_HOST_SHAPE.search("unifi:0a1b2c3d-1111-4222-8333-123456789abc.unifi-hosting")
    assert not _UUID_HOST_SHAPE.search("trace 0a1b2c3d-1111-4222-8333-123456789abc done")


def test_uuid_host_label_becomes_a_secret() -> None:
    h = Harvest()
    h.add_host("0a1b2c3d-1111-4222-8333-123456789abc.unifi-hosting.example.test", WORDS)
    assert "0a1b2c3d-1111-4222-8333-123456789abc" in h.secrets


def test_short_name_in_snake_case_key_is_code_but_in_prose_is_fatal() -> None:
    from obi.core.demo.gate import FATAL, LeakGate

    h = Harvest()
    h.add_person("Zev", WORDS)
    gate = LeakGate(h, dictionary=WORDS)
    out: list = []  # type: ignore[type-arg]
    gate._check_text("zev_window_events = 2000", "f", "l", identifier=False, out=out)
    assert not [f for f in out if f.severity == FATAL]
    gate._check_text("ask Zev about it", "f", "l", identifier=False, out=out)
    assert [f for f in out if f.severity == FATAL and f.category == "person"]


def test_technical_words_stay_ordinary() -> None:
    for word in ("max", "min", "mod"):
        assert is_dictionary_word(word, WORDS), word


def test_a_name_with_a_colon_is_a_name_not_an_address() -> None:
    h = Harvest()
    h.add_host("Wingnut: Switch - Gravelbox - 97:c4", WORDS)
    h.add_host("fd00::1", WORDS)
    assert "gravelbox" in h.place_words and "fd00::1" in h.ips
    assert not any("Gravelbox" in ip for ip in h.ips)


def test_asns_are_mapped_private_and_gated() -> None:
    from obi.core.demo.gate import _ASN_SHAPE, is_reserved_asn

    assert [int(m.group(1)) for m in _ASN_SHAPE.finditer("edge AS13335 and ASN 15169")] == [
        13335, 15169
    ]  # fmt: skip
    assert not is_reserved_asn(13335) and is_reserved_asn(64600)
    assert not _ASN_SHAPE.search("as 5 hops; CLASS 3; BASE64")


def test_gate_does_not_treat_time_zones_as_places() -> None:
    from obi.core.demo.gate import FATAL, LeakGate

    h = Harvest()
    h.place_words["spare"] = True  # a dictionary-word site
    gate = LeakGate(h, dictionary=WORDS)
    out: list = []  # type: ignore[type-arg]
    gate._check_text("time-zone-name=America/New_York", "f", "l", identifier=False, out=out)
    assert not [f for f in out if f.severity == FATAL]
    gate._check_text("ping spare-gw", "f", "l", identifier=False, out=out)
    assert [f for f in out if f.severity == FATAL]
