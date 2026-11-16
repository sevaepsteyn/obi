"""Unit tests: the site-tower placement rule (feature 097).

A site with a declared tower height (``[web] site_towers``) hosts ALL of its endpoints at
ONE shared point — the exported site record's position, at ground + tower height — with
``tower`` provenance on both axes and a named ``tower_placed`` correction per endpoint. The
declaration supersedes the 079 stray-pin anchor and the 083 height heuristic (one
correction, never two); unconfigured sites keep the existing rules bit-for-bit. Pure
function — hand-built records, no I/O.

Non-goal: a SAME-site PtP link (both ends on one tower) would collapse to zero length —
none exist in the live fleet, and the math stays finite (``fresnel_radius_m(0)`` is 0.0,
not NaN), so no guard and no test.
"""

from __future__ import annotations

import pytest

from obi.core.geo import build_geometry, merge_siklu_links
from obi.core.models import (
    Device,
    GeoDeviceRecord,
    GeoLinkRecord,
    GeometrySnapshot,
    GeoSiteRecord,
    HeightProvenance,
    LinkState,
    LocationProvenance,
    LogicalLink,
    Reachability,
    RfLink,
    SikluRadioStatus,
)
from tests.fakes import flat_ground

# Same coordinate discipline as the 079 tests: pins whose flat-earth offsets are exact,
# hand-checkable meters (1° lat ≈ 111194.9 m). FAR is beyond the 75 m sanity radius — the
# pin shape that 079 would anchor — so precedence is observable.
SITE_A = (40.0, -119.0)
SITE_B = (41.0, -118.0)
NEAR_LAT = 40.0000899  # ~10 m north of Site A (would be kept by 079)
FAR_LAT = 40.0015288  # ~170 m north of Site A (would be anchored by 079)

GROUND = 1190.0
TOWER_60FT = 60 * 0.3048  # 18.288 m — the operator's big-tower unit


def _device(
    name: str,
    *,
    site: str,
    lat: float,
    lon: float,
    alt: float | None = 1200.0,
) -> GeoDeviceRecord:
    return GeoDeviceRecord(
        name=name, site=site, latitude=lat, longitude=lon, altitude=alt,
        status="active", link_score=0.9,
    )


def _link(a: str, b: str, *, link_id: str = "L1") -> GeoLinkRecord:
    return GeoLinkRecord(id=link_id, from_name=a, to_name=b, link_type="wireless", state="active")


def _sites() -> list[GeoSiteRecord]:
    return [
        GeoSiteRecord(name="Site A", latitude=SITE_A[0], longitude=SITE_A[1]),
        GeoSiteRecord(name="Site B", latitude=SITE_B[0], longitude=SITE_B[1]),
    ]


def _build(
    devices: list[GeoDeviceRecord],
    links: list[GeoLinkRecord],
    sites: list[GeoSiteRecord],
    *,
    site_towers: dict[str, float] | None = None,
):
    return build_geometry(
        GeometrySnapshot(devices=devices, links=links, sites=sites),
        controller="https://uisp.example.com",
        ground=flat_ground(GROUND),
        amsl_tolerance=150.0,
        default_mast_height=10.0,
        degraded_link_score=0.5,
        site_towers=site_towers,
    )


def _endpoints_by_name(geometry) -> dict:
    return {e.name: e for e in geometry.endpoints}


# --- the shared point: every endpoint of a towered site is coincident ---------------------------


def test_towered_site_endpoints_share_one_point() -> None:
    # Two Site A radios with different pins AND different altitudes; peers at Site B.
    a1 = _device("a-ptp-b-60", site="Site A", lat=NEAR_LAT, lon=SITE_A[1], alt=1205.0)
    a2 = _device("a-ptp-c-60", site="Site A", lat=FAR_LAT, lon=SITE_A[1], alt=3.0)
    b1 = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    c1 = _device("c-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build(
        [a1, a2, b1, c1],
        [_link(a1.name, b1.name), _link(a2.name, c1.name, link_id="L2")],
        _sites(),
        site_towers={"Site A": TOWER_60FT},
    )

    ep1 = _endpoints_by_name(geometry)["a-ptp-b-60"]
    ep2 = _endpoints_by_name(geometry)["a-ptp-c-60"]
    for ep in (ep1, ep2):
        assert (ep.latitude, ep.longitude) == SITE_A  # the site record's position
        assert ep.height_amsl_m == pytest.approx(GROUND + TOWER_60FT, abs=0.01)
        assert ep.location_provenance is LocationProvenance.TOWER
        assert ep.height_provenance is HeightProvenance.TOWER
    # identical, not merely close — the whole point of the feature
    assert (ep1.latitude, ep1.longitude, ep1.height_amsl_m) == (
        ep2.latitude,
        ep2.longitude,
        ep2.height_amsl_m,
    )

    # ENU at the home site: exactly on the origin, up == the tower height
    site_a = next(s for s in geometry.sites if s.name == "Site A")
    assert site_a.enu["a-ptp-b-60"] == (0.0, 0.0, pytest.approx(TOWER_60FT, abs=0.01))

    # one named correction per towered endpoint, with the prior placement recorded
    assert [p.device for p in geometry.tower_placed] == ["a-ptp-b-60", "a-ptp-c-60"]
    placement = geometry.tower_placed[1]
    assert placement.site == "Site A"
    assert placement.tower_height_m == pytest.approx(TOWER_60FT)
    assert (placement.device_latitude, placement.device_longitude) == (FAR_LAT, SITE_A[1])
    assert placement.raw_altitude == 3.0
    assert (placement.site_latitude, placement.site_longitude) == SITE_A
    assert placement.height_amsl_m == pytest.approx(GROUND + TOWER_60FT, abs=0.01)
    assert placement.distance_m == pytest.approx(170.0, abs=0.5)
    assert geometry.tower_unmatched == []


# --- precedence: the declaration outranks 079 anchoring and the 083 height heuristic ------------


def test_tower_supersedes_the_079_anchor_one_correction_not_two() -> None:
    # FAR pin would site-anchor without a tower; with one, only tower_placed records it.
    a = _device("a-ptp-b-60", site="Site A", lat=FAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build([a, b], [_link(a.name, b.name)], _sites(), site_towers={"Site A": 5.0})

    ep = _endpoints_by_name(geometry)["a-ptp-b-60"]
    assert ep.location_provenance is LocationProvenance.TOWER
    assert geometry.site_anchored == []
    assert [p.device for p in geometry.tower_placed] == ["a-ptp-b-60"]


def test_tower_outranks_a_trusted_amsl_altitude() -> None:
    # 1205.0 is plausible AMSL (within tolerance of the 1190 ground) — 083 would keep it;
    # the declared mount height wins anyway.
    a = _device("a-ptp-b-60", site="Site A", lat=SITE_A[0], lon=SITE_A[1], alt=1205.0)
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build([a, b], [_link(a.name, b.name)], _sites(), site_towers={"Site A": 5.0})

    ep = _endpoints_by_name(geometry)["a-ptp-b-60"]
    assert ep.height_amsl_m == pytest.approx(GROUND + 5.0, abs=0.01)
    assert ep.height_provenance is HeightProvenance.TOWER
    assert ep.raw_altitude == 1205.0  # the overridden reading stays visible


# --- unconfigured sites keep the existing rules bit-for-bit -------------------------------------


def test_unconfigured_site_is_untouched() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=NEAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1], alt=1201.5)
    devices, links = [a, b], [_link(a.name, b.name)]

    towered = _build(devices, links, _sites(), site_towers={"Site A": TOWER_60FT})
    plain = _build(devices, links, _sites())

    towered_b = _endpoints_by_name(towered)["b-ptp-a-60"]
    plain_b = _endpoints_by_name(plain)["b-ptp-a-60"]
    assert towered_b == plain_b  # Site B: identical dataclass, field for field


def test_no_towers_is_a_no_op() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=NEAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    devices, links = [a, b], [_link(a.name, b.name)]

    empty = _build(devices, links, _sites(), site_towers={})
    omitted = _build(devices, links, _sites())
    assert empty.endpoints == omitted.endpoints
    assert empty.tower_placed == [] and empty.tower_unmatched == []


# --- neighboring towers stay distinct ------------------------------------------------------------


def test_two_configured_sites_keep_their_own_towers() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=NEAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build(
        [a, b], [_link(a.name, b.name)], _sites(),
        site_towers={"Site A": 30.48, "Site B": 2.0},
    )

    by_name = _endpoints_by_name(geometry)
    ep_a, ep_b = by_name["a-ptp-b-60"], by_name["b-ptp-a-60"]
    assert (ep_a.latitude, ep_a.longitude) == SITE_A
    assert (ep_b.latitude, ep_b.longitude) == SITE_B
    assert ep_a.height_amsl_m == pytest.approx(GROUND + 30.48, abs=0.01)
    assert ep_b.height_amsl_m == pytest.approx(GROUND + 2.0, abs=0.01)
    assert len(geometry.tower_placed) == 2


# --- an unmatched configured name degrades to a named report ------------------------------------


def test_unmatched_tower_site_is_reported_not_fatal() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=SITE_A[0], lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build(
        [a, b], [_link(a.name, b.name)], _sites(),
        site_towers={"Sight A": 18.0},  # typo'd name — matches nothing
    )

    assert geometry.tower_unmatched == ["Sight A"]
    assert geometry.tower_placed == []
    assert len(geometry.links) == 1  # export unharmed


# --- de-duplication: an endpoint shared by two links is corrected once --------------------------


def test_shared_endpoint_yields_one_correction() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=NEAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    c = _device("c-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build(
        [a, b, c],
        [_link(a.name, b.name), _link(a.name, c.name, link_id="L2")],
        _sites(),
        site_towers={"Site A": 5.0},
    )
    assert [p.device for p in geometry.tower_placed] == ["a-ptp-b-60"]


# --- Siklu merge: the e-band radio inherits its sibling's tower ---------------------------------


def _radio(identity: str) -> SikluRadioStatus:
    return SikluRadioStatus(
        device=Device(identity=identity, address=f"{identity}.net.example.org"),
        reachability=Reachability.REACHABLE,
        rf=RfLink(
            operational=True, air_capacity_mbps=5000,
            tx_frequency_mhz=81750, rx_frequency_mhz=71750,
        ),
    )


def test_siklu_endpoint_inherits_the_sibling_tower() -> None:
    a = _device("tamarack-ptp-basswood-60", site="Site A", lat=NEAR_LAT, lon=SITE_A[1])
    b = _device("basswood-ptp-tamarack-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build(
        [a, b], [_link(a.name, b.name)], _sites(), site_towers={"Site A": TOWER_60FT}
    )
    logical = LogicalLink(
        name="tamarack-ptp-basswood-70 ↔ basswood-ptp-tamarack-70",
        state=LinkState.UP,
        paired_by="name",
        end_a=_radio("tamarack-ptp-basswood-70"),
        end_b=_radio("basswood-ptp-tamarack-70"),
    )
    params = merge_siklu_links(geometry, [logical])
    assert params == {"merged": 1, "excluded": 0}

    by_name = _endpoints_by_name(geometry)
    merged, sibling = by_name["tamarack-ptp-basswood-70"], by_name["tamarack-ptp-basswood-60"]
    assert (merged.latitude, merged.longitude) == (sibling.latitude, sibling.longitude)
    assert merged.height_amsl_m == sibling.height_amsl_m
    assert merged.location_provenance is LocationProvenance.TOWER  # inherited, not defaulted
    assert merged.height_provenance is HeightProvenance.TOWER
    # nothing moved — the radio never had its own position, so no extra correction record
    assert [p.device for p in geometry.tower_placed] == ["tamarack-ptp-basswood-60"]
