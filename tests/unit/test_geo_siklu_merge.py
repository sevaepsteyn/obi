"""Unit tests: the Siklu e-band merge into the viewer geometry (044 US4, research R6).

``merge_siklu_links`` joins each logical-link end radio to the UISP world by the
naming-convention sibling (same ``<site>-ptp-<peer>-`` prefix, any band suffix), inherits
that endpoint's site/coordinates/height, and appends a ``source="siklu"`` link in the
70 GHz group. Unjoinable ends become named exclusions; the geometry is otherwise
untouched. Pure function — no I/O, no device contact.
"""

from __future__ import annotations

import pytest

from obi.core.geo import build_geometry, merge_siklu_links
from obi.core.models import (
    Device,
    FrequencyProvenance,
    GeoDeviceRecord,
    GeoLinkRecord,
    GeometrySnapshot,
    GeoSiteRecord,
    LinkHealth,
    LinkState,
    LocationProvenance,
    LogicalLink,
    Reachability,
    RfLink,
    SikluRadioStatus,
)
from tests.fakes import flat_ground


def _uisp_geometry():
    """A two-site UISP world with the 60 GHz siblings of the live e-band hop."""

    devices = [
        GeoDeviceRecord(
            name="tamarack-ptp-basswood-60", site="Tamarack - Tower",
            latitude=40.7712, longitude=-114.8273, altitude=1205.0, status="active",
            link_score=0.9,
        ),
        GeoDeviceRecord(
            name="basswood-ptp-tamarack-60", site="Basswood Relay - West",
            latitude=40.7519, longitude=-114.8462, altitude=1345.0, status="active",
            link_score=0.9,
        ),
    ]
    link = GeoLinkRecord(
        id="U1", from_name=devices[0].name, to_name=devices[1].name,
        link_type="wireless", state="active",
    )
    sites = [
        GeoSiteRecord(name="Tamarack - Tower", latitude=40.7712, longitude=-114.8273),
        GeoSiteRecord(name="Basswood Relay - West", latitude=40.7519, longitude=-114.8462),
    ]
    return build_geometry(
        GeometrySnapshot(devices=devices, links=[link], sites=sites),
        controller="https://uisp.example.com",
        ground=flat_ground(1190.0),
        amsl_tolerance=150.0,
        default_mast_height=10.0,
        degraded_link_score=0.5,
    )


def _radio(
    identity: str,
    *,
    reachable: bool = True,
    operational: bool | None = True,
    capacity_mbps: int | None = 5000,
    tx_mhz: int | None = 81750,
) -> SikluRadioStatus:
    rf = None
    if reachable:
        rf = RfLink(
            operational=operational,
            air_capacity_mbps=capacity_mbps,
            tx_frequency_mhz=tx_mhz,
            rx_frequency_mhz=71750 if tx_mhz else None,
        )
    return SikluRadioStatus(
        device=Device(identity=identity, address=f"{identity}.net.example.org"),
        reachability=Reachability.REACHABLE if reachable else Reachability.UNREACHABLE,
        rf=rf,
    )


def _logical(state: LinkState = LinkState.UP, **radio_kwargs) -> LogicalLink:
    a = _radio("basswood-ptp-tamarack-70", **radio_kwargs)
    b = _radio("tamarack-ptp-basswood-70", capacity_mbps=10000, tx_mhz=71750)
    return LogicalLink(
        name="basswood-ptp-tamarack-70 ↔ tamarack-ptp-basswood-70",
        state=state, paired_by="name", end_a=a, end_b=b,
    )


def test_up_link_merges_with_sibling_geometry() -> None:
    geometry = _uisp_geometry()
    params = merge_siklu_links(geometry, [_logical()])
    assert params == {"merged": 1, "excluded": 0}

    siklu = [link for link in geometry.links if link.source == "siklu"]
    assert len(siklu) == 1
    [link] = siklu
    assert link.band == "70 GHz"
    assert link.health is LinkHealth.HEALTHY
    assert link.maintenance is False
    assert {link.a, link.b} == {"basswood-ptp-tamarack-70", "tamarack-ptp-basswood-70"}
    assert "Tamarack - Tower" in link.name and "Basswood Relay - West" in link.name

    # endpoints inherit the sibling mast's site, coordinates, and resolved height
    by_name = {e.name: e for e in geometry.endpoints}
    merged = by_name["basswood-ptp-tamarack-70"]
    sibling = by_name["basswood-ptp-tamarack-60"]
    assert merged.site == sibling.site
    assert merged.latitude == sibling.latitude and merged.longitude == sibling.longitude
    assert merged.height_amsl_m == sibling.height_amsl_m

    # every site's ENU table gained both new endpoints
    for site in geometry.sites:
        assert "basswood-ptp-tamarack-70" in site.enu
        assert "tamarack-ptp-basswood-70" in site.enu

    # link length matches the sibling hop; Fresnel trio present from the device channel
    uisp = next(ln for ln in geometry.links if ln.source == "uisp")
    assert link.length_m == pytest.approx(uisp.length_m, abs=1.0)
    assert link.frequency_ghz == pytest.approx(81.75)
    assert link.frequency_provenance is FrequencyProvenance.DEVICE_CHANNEL
    assert link.fresnel_radius_m is not None and link.fresnel_radius_m > 0


def test_capacity_maps_per_end_and_usage_stays_absent() -> None:
    geometry = _uisp_geometry()
    merge_siklu_links(geometry, [_logical()])
    [link] = [ln for ln in geometry.links if ln.source == "siklu"]
    assert link.capacity_down_bps == 5000 * 1_000_000  # end A's air capacity
    assert link.capacity_up_bps == 10000 * 1_000_000  # end B's
    assert link.usage_down_bps is None and link.usage_up_bps is None


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (LinkState.UP, LinkHealth.HEALTHY),
        (LinkState.DEGRADED, LinkHealth.DEGRADED),
        (LinkState.DOWN, LinkHealth.DOWN),
    ],
)
def test_link_state_maps_to_viewer_health(state: LinkState, expected: LinkHealth) -> None:
    geometry = _uisp_geometry()
    merge_siklu_links(geometry, [_logical(state=state)])
    [link] = [ln for ln in geometry.links if ln.source == "siklu"]
    assert link.health is expected


def test_unknown_state_is_excluded_not_rendered_gray() -> None:
    """Acceptance scenario 2: unreachable radios yield NO e-band link, a named exclusion."""

    geometry = _uisp_geometry()
    params = merge_siklu_links(geometry, [_logical(state=LinkState.UNKNOWN)])
    assert params == {"merged": 0, "excluded": 1}
    assert not [ln for ln in geometry.links if ln.source == "siklu"]
    assert any("unreachable" in e.reason for e in geometry.excluded)


def test_missing_frequency_falls_back_to_the_band_nominal() -> None:
    geometry = _uisp_geometry()
    logical = _logical(tx_mhz=None)
    logical.end_b.rf.tx_frequency_mhz = None  # type: ignore[union-attr]
    merge_siklu_links(geometry, [logical])
    [link] = [ln for ln in geometry.links if ln.source == "siklu"]
    assert link.frequency_ghz == pytest.approx(73.0)
    assert link.frequency_provenance is FrequencyProvenance.BAND_NOMINAL


def test_no_sibling_is_a_named_exclusion_not_an_error() -> None:
    geometry = _uisp_geometry()
    orphan = LogicalLink(
        name="alder-ptp-nowhere-70 ↔ nowhere-ptp-alder-70",
        state=LinkState.UP, paired_by="name",
        end_a=_radio("alder-ptp-nowhere-70"), end_b=_radio("nowhere-ptp-alder-70"),
    )
    before_links = len(geometry.links)
    before_endpoints = len(geometry.endpoints)
    params = merge_siklu_links(geometry, [orphan])
    assert params == {"merged": 0, "excluded": 1}
    assert len(geometry.links) == before_links
    assert len(geometry.endpoints) == before_endpoints
    [excluded] = [e for e in geometry.excluded if "alder" in e.link]
    assert excluded.reason


def _half_known(present_name: str = "basswood-ptp-tamarack-70", *, present_on_a: bool = True,
                **radio_kwargs) -> LogicalLink:
    """A half-known e-band link: one radio read (``present``), the other dark (``None``).

    The logical name is the canonical pair name — the SAME name ``derive_links`` produces whether
    the link is fully-known or half-known — so a held link keeps the fully-known link's identity.
    """

    from obi.core.pairing import counterpart_name

    present = _radio(present_name, **radio_kwargs)
    peer = counterpart_name(present_name) or "?"
    return LogicalLink(
        name=f"{present_name} ↔ {peer}",
        state=LinkState.HALF_KNOWN, paired_by="name",
        end_a=present if present_on_a else None,
        end_b=None if present_on_a else present,
    )


def test_half_known_link_is_held_as_unknown() -> None:
    """071 US1: one radio unread → the link is HELD (health=unknown), not excluded, with the
    dark end grounded in its sibling mast by the name derived from the reachable peer."""

    geometry = _uisp_geometry()
    params = merge_siklu_links(geometry, [_half_known()])
    assert params == {"merged": 1, "excluded": 0}

    siklu = [ln for ln in geometry.links if ln.source == "siklu"]
    assert len(siklu) == 1
    [link] = siklu
    assert link.band == "70 GHz"
    assert link.health is LinkHealth.UNKNOWN
    assert {link.a, link.b} == {"basswood-ptp-tamarack-70", "tamarack-ptp-basswood-70"}
    assert not [e for e in geometry.excluded if "siklu" in e.link]

    # both endpoints registered in every site's ENU table, exactly as a fully-known link
    for site in geometry.sites:
        assert "basswood-ptp-tamarack-70" in site.enu
        assert "tamarack-ptp-basswood-70" in site.enu

    # the DARK end is grounded in its sibling mast and marked status "unknown" (it wasn't read)
    by_name = {e.name: e for e in geometry.endpoints}
    dark = by_name["tamarack-ptp-basswood-70"]
    assert dark.site == "Tamarack - Tower"
    assert dark.status == "unknown"


def test_half_known_link_with_the_dark_end_first_is_held() -> None:
    """Override-shaped half-known (end_a=None): the derivation/signal/frequency paths must not
    assume end_a is present — held all the same."""

    geometry = _uisp_geometry()
    params = merge_siklu_links(geometry, [_half_known(present_on_a=False)])
    assert params == {"merged": 1, "excluded": 0}
    [link] = [ln for ln in geometry.links if ln.source == "siklu"]
    assert link.health is LinkHealth.UNKNOWN
    assert {link.a, link.b} == {"basswood-ptp-tamarack-70", "tamarack-ptp-basswood-70"}


def test_held_link_shares_the_up_links_static_geometry() -> None:
    """FR-004 / SC-004: a held half-known link carries the SAME static geometry a fully-known
    link between the same masts does — it differs only in health (and the dark-end-dependent
    live fields), so the map curve and elevation profile render identically."""

    up_geo = _uisp_geometry()
    merge_siklu_links(up_geo, [_logical()])
    [up] = [ln for ln in up_geo.links if ln.source == "siklu"]

    held_geo = _uisp_geometry()
    merge_siklu_links(held_geo, [_half_known()])
    [held] = [ln for ln in held_geo.links if ln.source == "siklu"]

    assert held.id == up.id
    assert held.name == up.name
    assert {held.a, held.b} == {up.a, up.b}
    assert held.band == up.band
    assert held.length_m == up.length_m
    assert held.frequency_ghz == up.frequency_ghz
    assert held.frequency_provenance is up.frequency_provenance
    assert held.fresnel_radius_m == up.fresnel_radius_m
    # the ONE intended difference:
    assert up.health is LinkHealth.HEALTHY and held.health is LinkHealth.UNKNOWN


def test_half_known_with_a_nonconforming_present_name_is_excluded() -> None:
    """FR-005: if the reachable end's name doesn't follow the convention, the dark end's name
    can't be derived — the link stays excluded (never grounded on a guess)."""

    geometry = _uisp_geometry()
    half = LogicalLink(
        name="weirdname ↔ ?", state=LinkState.HALF_KNOWN, paired_by="override",
        end_a=_radio("weirdname"), end_b=None,
    )
    params = merge_siklu_links(geometry, [half])
    assert params == {"merged": 0, "excluded": 1}
    assert any("derive" in e.reason for e in geometry.excluded)


def test_half_known_when_the_derived_end_has_no_sibling_is_excluded() -> None:
    """FR-005: the reachable end grounds, but its derived peer has no sibling mast → excluded."""

    geometry = _uisp_geometry()
    half = _half_known("basswood-ptp-nowhere-70")  # counterpart nowhere-ptp-basswood-70 has no mast
    params = merge_siklu_links(geometry, [half])
    assert params == {"merged": 0, "excluded": 1}
    assert any("no UISP sibling mast" in e.reason for e in geometry.excluded)


def test_field_helpers_tolerate_a_dark_end() -> None:
    """T002: the None-tolerant field derivations used by the hold path."""

    from obi.core.geo import _siklu_capacity_bps, _siklu_frequency_mhz, _siklu_signal_dbm

    a = _radio("basswood-ptp-tamarack-70", capacity_mbps=5000, tx_mhz=81750)
    a.rf.rssi_dbm = -43.0  # type: ignore[union-attr]
    b = _radio("tamarack-ptp-basswood-70", capacity_mbps=10000, tx_mhz=71750)

    # one dark end: capacity mirrors the present end to both directions, frequency + signal from it
    assert _siklu_capacity_bps(a, None) == (5_000_000_000, 5_000_000_000)
    assert _siklu_capacity_bps(None, a) == (5_000_000_000, 5_000_000_000)
    assert _siklu_frequency_mhz(a, None) == (81750.0, FrequencyProvenance.DEVICE_CHANNEL)
    assert _siklu_signal_dbm(a, None) == -43.0
    assert _siklu_signal_dbm(None, a) == -43.0

    # both ends present: byte-identical to today (per-end down/up, end-A signal)
    assert _siklu_capacity_bps(a, b) == (5_000_000_000, 10_000_000_000)
    assert _siklu_signal_dbm(a, b) == -43.0


def test_ambiguous_fallback_join_is_deterministic() -> None:
    """No exact site+peer sibling: the leading-site-token fallback picks the
    lexicographically first candidate, run after run."""

    geometry = _uisp_geometry()
    logical = LogicalLink(
        name="basswood-ptp-elsewhere-70 ↔ tamarack-ptp-elsewhere-70",
        state=LinkState.UP, paired_by="name",
        end_a=_radio("basswood-ptp-elsewhere-70"),
        end_b=_radio("tamarack-ptp-elsewhere-70", capacity_mbps=10000, tx_mhz=71750),
    )
    merge_siklu_links(geometry, [logical])
    [link] = [ln for ln in geometry.links if ln.source == "siklu"]
    by_name = {e.name: e for e in geometry.endpoints}
    assert by_name["basswood-ptp-elsewhere-70"].site == "Basswood Relay - West"
    assert by_name["tamarack-ptp-elsewhere-70"].site == "Tamarack - Tower"
    assert link.band == "70 GHz"


def test_merge_from_the_recorded_siklu_fixture() -> None:
    """T002: the 020 fixture radios pair into the live e-band hop and merge cleanly."""

    from obi.core.drivers.siklu import parse_rf, parse_system
    from obi.core.pairing import derive_links
    from tests.fakes import load_siklu_fixture

    radios = []
    for name in ("tamarack-ptp-basswood-70", "basswood-ptp-tamarack-70"):
        commands = load_siklu_fixture(name)  # {show command -> raw output}
        system = parse_system(commands["show system"])
        radios.append(
            SikluRadioStatus(
                device=Device(identity=name, address=f"{name}.net.example.org"),
                reachability=Reachability.REACHABLE,
                location=system.get("location"),
                rf=parse_rf(commands["show rf"]),
            )
        )

    logical = derive_links(radios)
    geometry = _uisp_geometry()
    params = merge_siklu_links(geometry, logical)
    assert params["merged"] == 1
    [link] = [ln for ln in geometry.links if ln.source == "siklu"]
    assert link.band == "70 GHz"
    assert link.capacity_down_bps and link.capacity_down_bps >= 1_000_000_000


def test_siklu_endpoint_inherits_a_site_anchored_siblings_corrected_coordinate() -> None:
    """079 FR-013: when a UISP sibling mast is site-anchored, the merged Siklu endpoint inherits
    the CORRECTED coordinate — no Siklu-specific logic; the inheritance just reads the fixed pin."""

    site_pin = (40.7519, -114.8462)
    devices = [
        GeoDeviceRecord(
            name="tamarack-ptp-basswood-60", site="Tamarack - Tower",
            latitude=40.7712, longitude=-114.8273, altitude=1205.0, status="active",
            link_score=0.9,
        ),
        # this device pin has strayed ~111 m north of its site pin → it will be site-anchored
        GeoDeviceRecord(
            name="basswood-ptp-tamarack-60", site="Basswood Relay - West",
            latitude=site_pin[0] + 0.001, longitude=site_pin[1], altitude=1345.0,
            status="active", link_score=0.9,
        ),
    ]
    link = GeoLinkRecord(
        id="U1", from_name=devices[0].name, to_name=devices[1].name,
        link_type="wireless", state="active",
    )
    sites = [
        GeoSiteRecord(name="Tamarack - Tower", latitude=40.7712, longitude=-114.8273),
        GeoSiteRecord(name="Basswood Relay - West", latitude=site_pin[0], longitude=site_pin[1]),
    ]
    geometry = build_geometry(
        GeometrySnapshot(devices=devices, links=[link], sites=sites),
        controller="https://uisp.example.com",
        ground=flat_ground(1190.0),
        amsl_tolerance=150.0,
        default_mast_height=10.0,
        degraded_link_score=0.5,
    )
    sibling = next(e for e in geometry.endpoints if e.name == "basswood-ptp-tamarack-60")
    assert sibling.location_provenance is LocationProvenance.SITE  # corrected to the site pin
    assert (sibling.latitude, sibling.longitude) == site_pin

    merge_siklu_links(geometry, [_logical()])
    merged = next(e for e in geometry.endpoints if e.name == "basswood-ptp-tamarack-70")
    # the e-band endpoint inherits the sibling's CORRECTED coordinate, not the stale device pin
    assert (merged.latitude, merged.longitude) == site_pin
