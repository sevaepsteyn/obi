"""Unit tests: the site-anchoring placement rule (feature 079, US1/US3).

``build_geometry`` compares each endpoint's device pin to its site pin and, beyond
``location_sanity_m``, relocates the endpoint to the site pin — recording provenance and a named
correction. These tests exercise the rule matrix on hand-built records (a strayed device pin lives
in the recorded fixture, but the unit matrix stays deterministic and self-contained).
"""

from __future__ import annotations

import pytest

from obi.core.geo import build_geometry
from obi.core.models import (
    GeoDeviceRecord,
    GeoLinkRecord,
    GeometrySnapshot,
    GeoSiteRecord,
    LocationProvenance,
)
from tests.fakes import flat_ground

# Site pins and device pins chosen so the flat-earth distance is an exact round number of meters
# (1° lat ≈ 111194.9 m): NEAR = 10 m from Site A, FAR = 170 m, MID = 48 m. Longitude unchanged.
SITE_A = (40.0, -119.0)
SITE_B = (41.0, -118.0)
NEAR_LAT = 40.0000899  # ~10 m north of Site A (a rounded-copy device pin)
FAR_LAT = 40.0015288   # ~170 m north of Site A (a stale copy — the basswood-ptp-yew-05 shape)
MID_LAT = 40.0004317   # ~48 m north of Site A (the neighboring-site witness)


def _device(name: str, *, site: str, lat: float, lon: float, alt: float | None = 1200.0,
            status: str | None = "active") -> GeoDeviceRecord:
    return GeoDeviceRecord(
        name=name, site=site, latitude=lat, longitude=lon, altitude=alt,
        status=status, link_score=0.9,
    )


def _link(a: str, b: str, *, link_id: str = "L1") -> GeoLinkRecord:
    return GeoLinkRecord(id=link_id, from_name=a, to_name=b, link_type="wireless", state="active")


def _build(devices: list[GeoDeviceRecord], links: list[GeoLinkRecord],
           sites: list[GeoSiteRecord], *, location_sanity_m: float = 75.0):
    return build_geometry(
        GeometrySnapshot(devices=devices, links=links, sites=sites),
        controller="https://uisp.example.com",
        ground=flat_ground(1190.0),
        amsl_tolerance=150.0,
        default_mast_height=10.0,
        degraded_link_score=0.5,
        location_sanity_m=location_sanity_m,
    )


def _sites() -> list[GeoSiteRecord]:
    return [
        GeoSiteRecord(name="Site A", latitude=SITE_A[0], longitude=SITE_A[1]),
        GeoSiteRecord(name="Site B", latitude=SITE_B[0], longitude=SITE_B[1]),
    ]


def _endpoints_by_name(geometry) -> dict:
    return {e.name: e for e in geometry.endpoints}


# --- (a) far pin → snapped to the site pin, named ----------------------------------------------


def test_strayed_device_pin_is_snapped_to_the_site_pin() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=FAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build([a, b], [_link(a.name, b.name)], _sites())

    ep = _endpoints_by_name(geometry)["a-ptp-b-60"]
    assert ep.location_provenance is LocationProvenance.SITE
    assert (ep.latitude, ep.longitude) == SITE_A  # relocated to the site pin

    assert len(geometry.site_anchored) == 1
    anchor = geometry.site_anchored[0]
    assert anchor.device == "a-ptp-b-60"
    assert anchor.site == "Site A"
    assert anchor.distance_m == pytest.approx(170.0, abs=0.5)
    assert (anchor.device_latitude, anchor.device_longitude) == (FAR_LAT, SITE_A[1])
    assert (anchor.site_latitude, anchor.site_longitude) == SITE_A


# --- (b) near pin (rounded copy) → kept ---------------------------------------------------------


def test_near_device_pin_is_kept_as_device() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=NEAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build([a, b], [_link(a.name, b.name)], _sites())

    ep = _endpoints_by_name(geometry)["a-ptp-b-60"]
    assert ep.location_provenance is LocationProvenance.DEVICE
    assert (ep.latitude, ep.longitude) == (NEAR_LAT, SITE_A[1])  # device pin preserved
    assert geometry.site_anchored == []


# --- (c) device pin exactly on the site pin (distance 0) → kept --------------------------------


def test_device_pin_on_the_site_pin_is_kept() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=SITE_A[0], lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build([a, b], [_link(a.name, b.name)], _sites())

    ep = _endpoints_by_name(geometry)["a-ptp-b-60"]
    assert ep.location_provenance is LocationProvenance.DEVICE
    assert geometry.site_anchored == []


# --- (d) site pin missing → kept device (no basis to correct) -----------------------------------


def test_null_coord_site_excludes_the_link_and_never_corrects() -> None:
    # A site present in /sites but WITHOUT coordinates cannot be a viewer origin, so the link is
    # excluded by the existing pre-079 rule — the correction never fires (there is no site pin to
    # compare against). This confirms 079 leaves that path untouched.
    a = _device("a-ptp-b-60", site="Site A", lat=FAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    sites = [
        GeoSiteRecord(name="Site A", latitude=None, longitude=None),
        GeoSiteRecord(name="Site B", latitude=SITE_B[0], longitude=SITE_B[1]),
    ]
    geometry = _build([a, b], [_link(a.name, b.name)], sites)

    assert geometry.links == []  # excluded: Site A has no coordinates (unchanged pre-079 behavior)
    assert geometry.site_anchored == []


def test_site_absent_from_sites_list_keeps_the_device_pin() -> None:
    # Site A is referenced by the endpoint but absent from /sites entirely → synthesized at the
    # device → no site pin to correct against → device pin kept.
    a = _device("a-ptp-b-60", site="Site A", lat=FAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    sites = [GeoSiteRecord(name="Site B", latitude=SITE_B[0], longitude=SITE_B[1])]
    geometry = _build([a, b], [_link(a.name, b.name)], sites)

    ep = _endpoints_by_name(geometry)["a-ptp-b-60"]
    assert ep.location_provenance is LocationProvenance.DEVICE
    assert geometry.site_anchored == []


# --- (e) an endpoint referenced by two links is decided/recorded once ---------------------------


def test_endpoint_on_two_links_is_corrected_once() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=FAR_LAT, lon=SITE_A[1])  # strayed, shared end
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])  # at its site pin
    c = _device("c-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])  # at its site pin
    geometry = _build(
        [a, b, c],
        [_link(a.name, b.name, link_id="L1"), _link(a.name, c.name, link_id="L2")],
        _sites(),
    )
    # a is an endpoint of both links but corrected exactly once; b and c sit on their pins.
    assert [anchor.device for anchor in geometry.site_anchored] == ["a-ptp-b-60"]


# --- height is unchanged by a horizontal snap (FR-011) ------------------------------------------


def test_snap_is_horizontal_only_height_unchanged() -> None:
    a_far = _device("a-ptp-b-60", site="Site A", lat=FAR_LAT, lon=SITE_A[1], alt=1200.0)
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    snapped = _endpoints_by_name(_build([a_far, b], [_link(a_far.name, b.name)], _sites()))
    # alt 1200 vs ground 1190 is within amsl_tolerance ⇒ AMSL height 1200, regardless of the
    # horizontal correction. Same height a NEAR (kept) endpoint would resolve to.
    a_near = _device("a-ptp-b-60", site="Site A", lat=NEAR_LAT, lon=SITE_A[1], alt=1200.0)
    kept = _endpoints_by_name(_build([a_near, b], [_link(a_near.name, b.name)], _sites()))
    assert snapped["a-ptp-b-60"].height_amsl_m == kept["a-ptp-b-60"].height_amsl_m == 1200.0
    assert snapped["a-ptp-b-60"].height_provenance is kept["a-ptp-b-60"].height_provenance


# --- (US3) disable and tighten ------------------------------------------------------------------


def test_radius_zero_disables_the_correction() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=FAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build([a, b], [_link(a.name, b.name)], _sites(), location_sanity_m=0.0)

    ep = _endpoints_by_name(geometry)["a-ptp-b-60"]
    assert ep.location_provenance is LocationProvenance.DEVICE
    assert (ep.latitude, ep.longitude) == (FAR_LAT, SITE_A[1])
    assert geometry.site_anchored == []


def test_negative_radius_disables_the_correction() -> None:
    a = _device("a-ptp-b-60", site="Site A", lat=FAR_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    geometry = _build([a, b], [_link(a.name, b.name)], _sites(), location_sanity_m=-1.0)
    assert geometry.site_anchored == []


def test_tightening_the_radius_corrects_a_closer_endpoint() -> None:
    # At 48 m, the default 75 m keeps the endpoint; tightening below 48 m snaps it.
    a = _device("a-ptp-b-60", site="Site A", lat=MID_LAT, lon=SITE_A[1])
    b = _device("b-ptp-a-60", site="Site B", lat=SITE_B[0], lon=SITE_B[1])
    kept = _build([a, b], [_link(a.name, b.name)], _sites(), location_sanity_m=75.0)
    assert kept.site_anchored == []
    tightened = _build([a, b], [_link(a.name, b.name)], _sites(), location_sanity_m=40.0)
    assert [anchor.device for anchor in tightened.site_anchored] == ["a-ptp-b-60"]
    assert _endpoints_by_name(tightened)["a-ptp-b-60"].location_provenance is (
        LocationProvenance.SITE
    )
