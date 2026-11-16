"""Unit tests: ENU projection + per-site tables + the worked example (feature 022, T008/T020).

The projection is cross-checked two ways: against hand-computed values, and against the
controller's own per-link ``devicesDistance`` from the recorded data-links fixture (an
independent implementation of the same physics).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from obi.core.geo import build_geometry, enu_offset
from tests.fakes import bm_ground, geometry_snapshot

# 0.01° of latitude in meters on the IUGG mean-radius sphere: (0.01·π/180) · 6371008.8
_LAT_DEGREE_HUNDREDTH_M = 1111.9508


def test_enu_hand_computed_north() -> None:
    east, north = enu_offset(40.0, -119.0, 40.01, -119.0)
    assert east == pytest.approx(0.0, abs=1e-9)
    assert north == pytest.approx(_LAT_DEGREE_HUNDREDTH_M, rel=1e-4)


def test_enu_hand_computed_east_shrinks_with_latitude() -> None:
    east, north = enu_offset(40.0, -119.0, 40.0, -118.99)
    assert north == pytest.approx(0.0, abs=1e-9)
    assert east == pytest.approx(_LAT_DEGREE_HUNDREDTH_M * math.cos(math.radians(40.0)), rel=1e-4)


def test_enu_signs() -> None:
    east, north = enu_offset(40.0, -119.0, 39.99, -119.01)  # south-west of origin
    assert east < 0 and north < 0


def _fixture_geometry():
    return build_geometry(
        geometry_snapshot(),
        controller="https://uisp.example.com",
        ground=bm_ground(),
        amsl_tolerance=150.0,
        default_mast_height=10.0,
        degraded_link_score=0.5,
    )


def test_link_lengths_match_controller_sites_distance() -> None:
    """obi's link lengths vs the controller's ``sitesDistance`` (fixture-verified: the
    ``devicesDistance`` field is null across the live fleet, so the site-to-site figure is
    the controller's usable independent computation of the same geometry).

    Devices sit near — not at — their site markers, so short links are skipped and the band
    is loose (25% or 400 m): this catches a projection/axis error by orders of magnitude
    without chasing the controller's exact model. Precision is covered by the haversine test.
    """

    geometry = _fixture_geometry()
    snapshot = geometry_snapshot()
    controller_distance = {
        link.id: link.site_distance_m for link in snapshot.links if link.site_distance_m
    }
    compared = 0
    for link in geometry.links:
        expected = controller_distance.get(link.id)
        if not expected or expected < 1000:  # short hops: site-marker offset dominates
            continue
        tolerance = max(0.25 * expected, 400.0)
        assert link.length_m == pytest.approx(expected, abs=tolerance), link.name
        compared += 1
    assert compared >= 3  # the fixture must actually exercise the cross-check


def test_link_lengths_match_independent_haversine() -> None:
    """Precision cross-check: an independent great-circle implementation of every link.

    Haversine + Δh is a different formula from the production equirectangular projection;
    at ≤20 km the two must agree to well under 0.5%.
    """

    geometry = _fixture_geometry()
    by_name = {endpoint.name: endpoint for endpoint in geometry.endpoints}
    radius = 6_371_008.8
    for link in geometry.links:
        a, b = by_name[link.a], by_name[link.b]
        lat_a, lat_b = math.radians(a.latitude), math.radians(b.latitude)
        d_lat = lat_b - lat_a
        d_lon = math.radians(b.longitude - a.longitude)
        haversine = 2 * radius * math.asin(
            math.sqrt(
                math.sin(d_lat / 2) ** 2
                + math.cos(lat_a) * math.cos(lat_b) * math.sin(d_lon / 2) ** 2
            )
        )
        expected = math.sqrt(haversine**2 + (b.height_amsl_m - a.height_amsl_m) ** 2)
        assert link.length_m == pytest.approx(expected, rel=0.005, abs=1.0), link.name


def test_every_site_enu_table_covers_every_endpoint() -> None:
    geometry = _fixture_geometry()
    endpoint_names = {endpoint.name for endpoint in geometry.endpoints}
    assert geometry.sites, "fixture produced no sites"
    for site in geometry.sites:
        assert set(site.enu.keys()) == endpoint_names


def test_enu_up_is_relative_to_the_origin_site_ground() -> None:
    geometry = _fixture_geometry()
    by_name = {endpoint.name: endpoint for endpoint in geometry.endpoints}
    for site in geometry.sites[:5]:
        for name, (_east, _north, up) in site.enu.items():
            assert up == pytest.approx(
                by_name[name].height_amsl_m - site.ground_elevation_m, abs=0.02
            )


WORKED_EXAMPLE_PATH = Path(__file__).parents[1] / "fixtures" / "ar" / "worked-example.json"


def _worked_example() -> dict:
    """The NOC-origin worked example (contracts/ar-viewer.md): deterministic regeneration.

    Scene mapping is the viewer's FIXED transposition: x = east, y = up, z = −north.
    """

    geometry = _fixture_geometry()
    noc = next(s for s in geometry.sites if s.name == "BRC - NOC")
    pair = {"noc-ptp-basswood-60", "basswood-ptp-noc-60"}
    link = next(li for li in geometry.links if {li.a, li.b} == pair)

    def scene(name: str) -> dict:
        east, north, up = noc.enu[name]
        return {"x": east, "y": up, "z": -north}

    # 025 (contracts/ar-viewer-fresnel.md): the zone surfaces the viewer must compose from
    # the exported radius — full first zone (D/2, r, r) and the 0.6 clearance core with the
    # hair-shorter major axis. Scene D equals length_m up to export rounding; the worked
    # example uses length_m so the JS is checkable against one number.
    assert link.fresnel_radius_m is not None and link.frequency_ghz is not None
    radius = link.fresnel_radius_m
    half = link.length_m / 2

    return {
        "origin_site": noc.name,
        "link": link.name,
        "health": link.health.value,
        "label_km": round(link.length_m / 1000, 1),
        "scene": {link.a: scene(link.a), link.b: scene(link.b)},
        "frequency_ghz": link.frequency_ghz,
        "fresnel_radius_m": radius,
        "label": f"{link.name} · {round(link.length_m / 1000, 1)} km · F1 {radius:.1f} m",
        "zone_scale_full": [round(half, 1), radius, radius],
        "zone_scale_core": [
            round(half * 0.999, 1),
            round(radius * 0.6, 2),
            round(radius * 0.6, 2),
        ],
    }


def test_worked_example_matches_committed_fixture() -> None:
    """The committed worked-example fixture equals deterministic regeneration (T020).

    ``viewer.js`` and ``contracts/ar-viewer.md`` reference the committed file; this test
    guarantees it can never drift from the core's math.
    """

    regenerated = _worked_example()
    committed = json.loads(WORKED_EXAMPLE_PATH.read_text())
    assert committed == regenerated


def test_worked_example_shape_is_sane() -> None:
    example = _worked_example()
    noc_end = example["scene"]["noc-ptp-basswood-60"]
    mountain_end = example["scene"]["basswood-ptp-noc-60"]
    # The NOC radio is essentially at the origin site; the basswood is kilometers west
    # (negative x) and far above the playa (large positive y).
    assert abs(noc_end["x"]) < 200 and abs(noc_end["z"]) < 200
    assert mountain_end["x"] < -4000
    assert mountain_end["y"] > 100
    assert 4.0 < example["label_km"] < 8.0
    # 025: this 60 GHz link resolves its exact channel (65.88 GHz) and a ~2.6 m first zone
    # (research §2: 65.88 GHz @ ~5.8 km → 2.57 m); label carries the F1 suffix.
    assert example["frequency_ghz"] == pytest.approx(65.88)
    assert 2.0 < example["fresnel_radius_m"] < 3.2
    assert example["label"].endswith(f"F1 {example['fresnel_radius_m']:.1f} m")
    assert example["zone_scale_full"][1] == example["fresnel_radius_m"]
    assert example["zone_scale_core"][1] == pytest.approx(
        example["fresnel_radius_m"] * 0.6, abs=0.01
    )
