"""Unit tests for the rewriter (feature 134, T021/T024/T046)."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from obi.core.demo.harvest import Harvest, harvest
from obi.core.demo.mapping import PseudonymMap
from obi.core.demo.relocate import Relocator, centroid
from obi.core.demo.rewrite import EMAIL_MARK, PHONE_MARK, REDACTED, Rewriter
from obi.core.demo.timeshift import TimeShift
from tests.fixtures.demo.build_state import BUSY_END, BUSY_START, FIXTURE_DIR, build_state

WORDS = frozenset({"orchard", "relay", "trailer", "printer", "back", "handheld"})


@pytest.fixture(scope="module")
def harvested(tmp_path_factory: pytest.TempPathFactory) -> Harvest:
    root = tmp_path_factory.mktemp("state")
    build_state(root)
    raw = tomllib.loads((FIXTURE_DIR / "fixture.toml").read_text())
    window = (int(BUSY_START.timestamp() * 1e6), int(BUSY_END.timestamp() * 1e6))
    return harvest(raw, root / "obi", window, FIXTURE_DIR / "denylist.txt", WORDS)


def make_rewriter(h: Harvest) -> Rewriter:
    m = PseudonymMap(b"\x05" * 32)
    m.forbid(h.forbidden_tokens())
    tz = ZoneInfo("America/Los_Angeles")
    shift = TimeShift.from_window(BUSY_START.astimezone(tz), date(2025, 6, 2), tz)
    reloc = Relocator(centroid(h.site_coords), (37.16, -118.27), 0.0)
    return Rewriter(m, h, shift, reloc)


@pytest.fixture
def rw(harvested: Harvest) -> Rewriter:
    return make_rewriter(harvested)


# --- T021: secrets ----------------------------------------------------------------------------


def test_routeros_export_secrets_redacted(rw: Rewriter) -> None:
    export = (
        "/snmp community set [ find default=yes ] name=fakecommunity\n"
        "/snmp set trap-community=fakecommunity\n"
        "/user add name=pat password=fakepass99 group=full\n"
        '/ppp secret add name=client password="p p" service=pppoe\n'
        '/interface wifi set [ find ] security.passphrase="wifi-fake-passphrase"\n'
    )
    out = rw.export(export)
    for secret in ("fakecommunity", "fakepass99", "wifi-fake-passphrase", '"p p"'):
        assert secret not in out
    assert out.count(REDACTED) >= 5


def test_records_with_sensitive_keys(rw: Rewriter) -> None:
    out = rw.json({"password": "x1x1x1", "api_key": "abc", "comment": "token=zzz9"})
    assert out["password"] == REDACTED and out["api_key"] == REDACTED
    assert "zzz9" not in out["comment"]


def test_contact_and_token_shapes(rw: Rewriter) -> None:
    text = (
        "dm U0FAKE0001 mail pat@fictional-org.test call +1 555 010 4477 key sk-ant-abc123 "
        "bot xoxb-1-2-3 -----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----"
    )
    out = rw.text(text)
    assert "U0FAKE0001" not in out and "admin" in out
    assert EMAIL_MARK in out and PHONE_MARK in out
    assert "sk-ant" not in out and "xoxb" not in out and "AAA" not in out


# --- T024: identity passes --------------------------------------------------------------------


def test_ips_with_cidr_in_text(rw: Rewriter) -> None:
    out = rw.text("route 10.99.1.0/24 via 10.99.0.1 and v6 2600:1700:abcd:1::5/64")
    for literal in ("10.99", "2600:"):
        assert literal not in out
    assert "/24" in out and "/64" in out


@pytest.mark.parametrize(
    "mac", ["4C:5E:0C:11:22:01", "4c-5e-0c-11-22-01", "4c5e.0c11.2201", "4C5E0C112201"]
)
def test_every_mac_spelling(rw: Rewriter, mac: str) -> None:
    rw.text("seen 4C:5E:0C:11:22:01")  # a bare spelling is only rewritten once the MAC is known
    out = rw.text(f"client {mac} joined")
    assert mac not in out
    fake = out.split()[1]
    assert len(fake) == len(mac)
    assert fake.replace(":", "").replace("-", "").replace(".", "")[1] in "26ae26AE"


def test_url_hosts_and_fqdns(rw: Rewriter) -> None:
    out = rw.text("see https://uisp.fictional-org.test/nms and northfield-gw.fictional-org.test")
    assert "fictional-org" not in out and "northfield" not in out
    assert "uisp.example.net" in out


def test_longest_first_and_role_tokens(rw: Rewriter) -> None:
    a = rw.ident("eastmoor-gw")
    b = rw.ident("eastmoor-internet-gw")
    assert a.endswith("-gw") and b.endswith("-internet-gw")
    assert a.split("-")[0] == b.split("-")[0] != "eastmoor"
    assert rw.ident("core-gw-1") == "core-gw-1"


def test_word_boundaries_in_prose(rw: Rewriter) -> None:
    out = rw.text("northfieldish weather is fine; Northfield crew; the northern field")
    assert "northfield" not in out.casefold()  # glued names are rewritten like the gate sees them
    assert "the northern field" in out


def test_dictionary_word_places_only_in_identifier_context(rw: Rewriter) -> None:
    out = rw.text("orchard-ap rebooted; host=orchard; the back orchard printer and the core dump")
    assert "orchard-ap" not in out and "host=orchard" not in out
    assert "back orchard printer" in out and "core dump" in out


def test_oids_and_versions_byte_identical(rw: Rewriter) -> None:
    text = "oid 1.3.6.1.4.1.14988.1.1.1 polled; RouterOS 7.16.2 build 1.2.3.4.5"
    assert rw.text(text) == text


def test_people_nodes_serials(rw: Rewriter) -> None:
    out = rw.text("Quillon reported !0fa4e001 (Quillon Handheld) serial HZ90A1B2C3D")
    for real in ("Quillon", "!0fa4e001", "HZ90A1B2C3D"):
        assert real not in out
    assert "node-01" in out


def test_coordinates_and_times(rw: Rewriter) -> None:
    out = rw.text("fix 45.0123,-100.0456 at 2026-08-27T09:30:00-07:00")
    assert "45.0123" not in out and "2026" not in out
    lat = float(out.split()[1].split(",")[0])
    assert 36 < lat < 38


def test_json_identifier_keys_and_relocation(rw: Rewriter) -> None:
    out = rw.json(
        {
            "10.99.0.1": {"x": 1.0},
            "items": [
                {"name": "orchard-ap", "address": "10.99.4.20", "since_us": 1_788_000_000_000_000}
            ],
            "site": {"latitude": 45.04, "longitude": -100.03},
        }
    )
    key = next(iter(out))
    assert ipaddress.ip_address(key) in ipaddress.ip_network("172.16.0.0/12")
    item = out["items"][0]
    assert "orchard" not in item["name"] and item["name"].endswith("-ap")
    assert item["since_us"] != 1_788_000_000_000_000
    assert 36 < out["site"]["latitude"] < 38


# --- T046: event terms ------------------------------------------------------------------------


def test_event_terms_become_generic(rw: Rewriter) -> None:
    out = rw.text("dust on the playa near Esplanade by Fictional Camp and Center Camp")
    low = out.casefold()
    for term in ("playa", "esplanade", "fictional camp", "center camp"):
        assert term not in low
    assert "valley floor" in low and "main road" in low and "town center" in low


def test_mesh_near_art_suffix(rw: Rewriter) -> None:
    out = rw.text("7:30 & Esplanade, near Imaginary Sculpture")
    assert "imaginary" not in out.casefold()


def test_rewriter_is_deterministic(harvested: Harvest, tmp_path: Path) -> None:
    text = "northfield-gw 10.99.1.1 4C:5E:0C:11:22:01 Quillon"
    assert make_rewriter(harvested).text(text) == make_rewriter(harvested).text(text)


def test_camelcase_and_glued_names_in_dhcp_hostnames(rw: Rewriter) -> None:
    out = rw.text("lease for SouthgateNorthfield-TV and northfieldeastmoorcam and OfficeCam")
    low = out.casefold()
    for real in ("southgate", "northfield", "eastmoor"):
        assert real not in low


def test_near_misses_of_places_and_people_are_rewritten(rw: Rewriter) -> None:
    out = rw.text("mcast group for quillnn and eastmor registered")
    assert "quillnn" not in out and "eastmor" not in out


def test_event_pass_never_touches_hostnames_urls_or_generic_words(rw: Rewriter) -> None:
    out = rw.text("uisp-controller:https://uisp.fictional-org.test/x router on the playa")
    assert out.startswith("uisp-controller:https://uisp.example.net/x router on the valley floor")


def test_repeated_separators_are_still_identifiers(rw: Rewriter) -> None:
    out = rw.text("interface=ether11-orchard--sw-b comment=office__uplink")
    assert "orchard" not in out


def test_bare_key_field_is_an_identifier_not_a_secret(rw: Rewriter) -> None:
    out = rw.json({"key": "northfield-gw", "api_key": "abc", "ssh-key": "def"})
    assert out["key"].endswith("-gw") and "northfield" not in out["key"]
    assert out["api_key"] == REDACTED and out["ssh-key"] == REDACTED


def test_person_names_never_rewritten_inside_dotted_names(rw: Rewriter) -> None:
    out = rw.text("Quillon checked console.quillon.example.com")
    assert out.startswith("admin checked") or out.startswith("user-")
    assert ".example.com" in out


def test_routeros_comments_are_identifiers_and_wireguard_values_scrubbed(rw: Rewriter) -> None:
    # 136 publish check: a dictionary-word place in a comment, a peer person, a public key.
    out = rw.export(
        '/ip route add gateway=10.99.0.1 comment="northfield orchard relay"\n'
        '/interface wireguard peers add comment="marlo quartzbook" name=marlo-quartzbook '
        'public-key="Zm9vYmFyYmF6cXV1eGNvcmdlZ3JhdWx0Z2FycGx5d2E="\n'
    )
    assert "orchard" not in out and "northfield" not in out
    assert "marlo" not in out and "quartzbook" not in out
    assert "Zm9vYmFy" not in out and "public-key=<redacted-by-obi>" in out


def test_dhcp_client_hostnames_and_handles_are_pseudonymized(rw: Rewriter) -> None:
    out = rw.text("access assigned 10.99.1.57 for 4C:5E:0C:AA:BB:01 Tamsin-s-Phone-14")
    assert "Tamsin" not in out and "client-" in out
    again = rw.text("access deassigned 10.99.1.57 for 4C:5E:0C:AA:BB:01 Tamsin-s-Phone-14")
    assert again.split()[-1] == out.split()[-1]  # stable pseudonym
    note = rw.text('comment="@gravelbox laptop" mail pat@fictional-org.test')
    assert "gravelbox" not in note and "<email>" in note
    assert re.search(r'"@(admin|user-\d+) laptop"', note)


def test_asns_become_private(rw: Rewriter) -> None:
    out = rw.text("aspen-internet-gw is the AS13335 edge; peer ASN 15169; lab AS64600")
    numbers = [int(n) for n in re.findall(r"AS ?N? ?(\d+)", out)]
    assert 13335 not in numbers and 15169 not in numbers and 64600 in numbers
    assert all(64512 <= n <= 65534 for n in numbers)
    assert rw.text("AS13335") == rw.text("AS13335")  # stable


def test_compound_name_keys_and_time_zones_are_not_identifiers(harvested: Harvest) -> None:
    harvested.place_words.setdefault("spare", True)  # a dictionary-word site (as on the live run)
    try:
        rw = make_rewriter(harvested)
        line = "/system clock set time-zone-name=America/New_York"
        assert rw.text(line) == line
        assert "spare" not in rw.text("ping spare-gw").split()[-1]  # still a site in a hostname
    finally:
        del harvested.place_words["spare"]
