"""Unit (feature 111): BRC address geometry — plaza-fit orientation, ring naming, forms.

Runs against a SYNTHETIC survey city generated with the 2026 measured constants (12:00
axis 45° true; Esplanade 2311 / A 2705 / B 3213 / G 4822 ft), the licensed dataset stays
un-vendored (the 098 fixture philosophy). The synthetic city reproduces the two live
gotchas of 2026-08-28: the radial-corridor ray-cast degeneracy at 3:00, and the extra
unnamed ring near Center Camp that makes letter assignment ambiguous on the 6:15 radial —
where the tool must fall back to the dial form rather than guess a street name.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from obi.core.brc import BrcSource
from obi.core.brcaddr import BrcAddress, CityModel, derive_city_model, to_address
from tests.fixtures.brc.origin import ORIGIN, SPIKE_LAT, SPIKE_LON, YEAR

_EARTH_RADIUS_M = 6_371_008.8
_M_PER_FT = 0.3048
_AZ12 = 45.0

# TUMA's real position request answer (2026-08-28): bearing 232.86°, 2672 ft from the Man.
_TUMA = (40.7788198, -114.8232908)

# Centerline radii (ft): Esplanade, A, B, C, D, E, F, G — the measured city.
_RINGS_FT = [2311.0, 2705.0, 3213.0, 3493.0, 3772.0, 4057.0, 4541.0, 4822.0]
_EXTRA_RING_FT = 2933.0  # the Center Camp-zone extra, present only near 6:00


def _lonlat(bearing_deg: float, radius_ft: float) -> list[float]:
    east = radius_ft * _M_PER_FT * math.sin(math.radians(bearing_deg))
    north = radius_ft * _M_PER_FT * math.cos(math.radians(bearing_deg))
    lat = SPIKE_LAT + math.degrees(north / _EARTH_RADIUS_M)
    lon = SPIKE_LON + math.degrees(east / (_EARTH_RADIUS_M * math.cos(math.radians(SPIKE_LAT))))
    return [lon, lat]


def _latlon_at(bearing_deg: float, radius_ft: float) -> tuple[float, float]:
    lon, lat = _lonlat(bearing_deg, radius_ft)
    return lat, lon


def _arc(
    radius_ft: float, start_deg: float, end_deg: float, step: float = 0.5
) -> list[list[float]]:
    points = []
    b = start_deg
    while b <= end_deg + 1e-9:
        points.append(_lonlat(b, radius_ft))
        b += step
    return points


def _ring_arcs(centerline_ft: float, spans: list[tuple[float, float]]) -> list[list[list[float]]]:
    """A street ring as its two edge polylines (±6 m ≈ ±20 ft), over the given bearing spans."""

    half_ft = 6.0 / _M_PER_FT
    arcs = []
    for start, end in spans:
        arcs.append(_arc(centerline_ft - half_ft, start, end))
        arcs.append(_arc(centerline_ft + half_ft, start, end))
    return arcs


def _plaza(name: str, bearing_deg: float, radius_ft: float) -> dict[str, object]:
    ring = [
        _lonlat(bearing_deg + offset, radius_ft + wobble)
        for offset, wobble in ((-0.4, 0.0), (0.0, 80.0), (0.4, 0.0), (0.0, -80.0), (-0.4, 0.0))
    ]
    return {
        "type": "Feature",
        "properties": {"name": name},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def _write_city(cache_dir: Path, *, with_plazas: bool = True, with_streets: bool = True) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    if with_streets:
        # Full-circle rings, except a corridor gap on the 3:00 radial (the degeneracy) —
        # the city spans bearings 90°..360° (2:00..10:00) like the real survey. The gap's
        # half-width matches a real street's angular size (~±0.35° at these radii), so the
        # ±0.5° nudge clears it exactly as it does live.
        spans = [(90.0, 134.65), (135.35, 360.0)]
        arcs: list[list[list[float]]] = []
        for radius in _RINGS_FT:
            arcs.extend(_ring_arcs(radius, spans))
        # The Center Camp-zone extra ring: only near 6:00 (bearings 215°..245°).
        arcs.extend(_ring_arcs(_EXTRA_RING_FT, [(215.0, 245.0)]))
        streets = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "MultiLineString", "coordinates": arcs},
                }
            ],
        }
        (cache_dir / "street_outlines.geojson").write_text(json.dumps(streets))
    if with_plazas:
        plazas = {
            "type": "FeatureCollection",
            "features": [
                _plaza("3:00 & B Plaza", 135.0, 3213.0),
                _plaza("9:00 & B Plaza", 315.0, 3213.0),
                _plaza("3:00 & G Plaza", 135.0, 4822.0),
                _plaza("9:00 & G Plaza", 315.0, 4822.0),
                _plaza("Center Camp Plaza", 225.0, 2996.0),  # no clock/letter — skipped by the fit
            ],
        }
        (cache_dir / "plazas.geojson").write_text(json.dumps(plazas))


def _named_feature(
    name: str, radius_ft: float, spans: list[tuple[float, float]], kind: str | None
) -> dict[str, object]:
    return {
        "type": "Feature",
        "properties": {"name": name, "kind": kind, "width_ft": 30},
        "geometry": {
            "type": "MultiLineString",
            "coordinates": [_arc(radius_ft, start, end) for start, end in spans],
        },
    }


def _write_named_streets(cache_dir: Path) -> None:
    """The named-centerline layer (street_lines.geojson), matching the real 2026 survey's
    shape near Center Camp: rings ESP/A/B/C, Route 66 only around 6:00, radial avenues."""

    city = [(90.0, 360.0)]
    features = [
        _named_feature("ESP", 2311.0, city, "annular"),
        _named_feature("A", 2933.0, city, "annular"),
        _named_feature("B", 3213.0, city, "annular"),
        _named_feature("C", 3493.0, city, "annular"),
        # The Center Camp ring: present only near 6:00, kind unset in the real data.
        _named_feature("Route 66", 2705.0, [(215.0, 245.0)], None),
        # The Center Camp circle ("Rods Road"): NOT concentric with the Man, so its near
        # and far sides can both land within a block of one fix — two features standing in
        # for the real survey's six.
        _named_feature("Rods Road", 2150.0, [(215.0, 245.0)], "center_camp"),
        _named_feature("Rods Road", 2450.0, [(215.0, 245.0)], "center_camp"),
        # A radial avenue: its name is a clock time; never a corner candidate.
        {
            "type": "Feature",
            "properties": {"name": "6:00", "kind": "avenue", "width_ft": 40},
            "geometry": {
                "type": "LineString",
                "coordinates": [_lonlat(225.0, r) for r in (2311.0, 3000.0, 3600.0)],
            },
        },
    ]
    (cache_dir / "street_lines.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features})
    )


def _model(tmp_path: Path, *, named: bool = False, **kwargs: bool) -> CityModel:
    cache = tmp_path / "brc-2026"
    _write_city(cache, **kwargs)
    if named:
        _write_named_streets(cache)

    def _dead_fetcher(name: str) -> bytes | None:
        # Fetching a file the cache HOLDS would mean the cache was bypassed; a genuinely
        # absent file legitimately falls through to the fetcher (the BrcSource contract)
        # and gets a quiet "unavailable".
        if (cache / name).is_file():
            raise AssertionError(f"cache bypassed for {name}")
        return None

    return derive_city_model(BrcSource(cache, origin=ORIGIN, year=YEAR, fetcher=_dead_fetcher))


# --- orientation -------------------------------------------------------------------------


def test_plaza_fit_orientation(tmp_path: Path) -> None:
    model = _model(tmp_path)
    assert model.orientation_source == "plaza-fit"
    assert model.az12_deg == pytest.approx(_AZ12, abs=0.2)


def test_missing_plazas_fall_back_to_the_measured_constant(tmp_path: Path) -> None:
    model = _model(tmp_path, with_plazas=False)
    assert model.orientation_source == "fallback"
    assert model.az12_deg == 45.0


# --- corner form -------------------------------------------------------------------------


def test_corner_on_an_anchored_ring(tmp_path: Path) -> None:
    model = _model(tmp_path)
    lat, lon = _latlon_at(180.0, 3213.0)  # 4:30 & B, dead on
    address = to_address(lat, lon, model)
    assert address.form == "corner"
    assert address.text == "4:30 & B"
    assert address.street == "B"
    assert address.clock == "4:30"


def test_corner_on_an_inferred_ring_within_tolerance(tmp_path: Path) -> None:
    model = _model(tmp_path)
    lat, lon = _latlon_at(180.0, 2705.0 + 15.0)  # 15 ft off A's centerline
    address = to_address(lat, lon, model)
    assert address.form == "corner"
    assert address.text == "4:30 & A"


def test_corner_between_anchors_by_count_consistency(tmp_path: Path) -> None:
    lat, lon = _latlon_at(180.0, 3493.0)  # C: first of the 4 rings between anchors B and G
    address = to_address(lat, lon, _model(tmp_path))
    assert address.form == "corner"
    assert address.text == "4:30 & C"


def test_corner_on_esplanade(tmp_path: Path) -> None:
    lat, lon = _latlon_at(270.0, 2311.0)  # 7:30 & Esplanade
    address = to_address(lat, lon, _model(tmp_path))
    assert address.form == "corner"
    assert address.text == "7:30 & Esplanade"


def test_mid_block_is_dial_not_a_stretched_corner(tmp_path: Path) -> None:
    lat, lon = _latlon_at(180.0, 3353.0)  # 140 ft from both B and C
    address = to_address(lat, lon, _model(tmp_path))
    assert address.form == "dial"


# --- the two live gotchas ----------------------------------------------------------------


def test_tuma_gets_the_dial_form_because_the_extra_ring_makes_letters_ambiguous(
    tmp_path: Path,
) -> None:
    # On the 6:15 radial TWO rings sit between Esplanade and anchor B (A + the Center
    # Camp-zone extra) — one letter slot, two rings. A wrong street name is worse than no
    # street name, so the answer is the dial form.
    address = to_address(*_TUMA, _model(tmp_path))
    assert address.form == "dial"
    assert address.text == "6:15 and 2,670 feet from the Man"
    assert address.clock == "6:15"
    assert address.distance_ft == 2670


def test_radial_corridor_degeneracy_is_nudged(tmp_path: Path) -> None:
    # A ray aimed exactly down the 3:00 corridor gap crosses (almost) nothing — the live
    # 2026-08-28 discovery. Ring detection must nudge the bearing; the fix's own clock and
    # radius are unaffected.
    model = _model(tmp_path)
    lat, lon = _latlon_at(135.0, 3213.0)  # exactly 3:00, on B's centerline
    address = to_address(lat, lon, model)
    assert address.form == "corner"
    assert address.text == "3:00 & B"
    mid_lat, mid_lon = _latlon_at(135.0, 3000.0)
    mid = to_address(mid_lat, mid_lon, model)
    assert mid.form == "dial"
    assert mid.text == "3:00 and 3,000 feet from the Man"


# --- rounding ----------------------------------------------------------------------------


def test_dial_rounding_five_minutes_ten_feet(tmp_path: Path) -> None:
    lat, lon = _latlon_at(181.3, 2999.0)  # 4:32.6 → 4:35; 2999 → 3,000
    address = to_address(lat, lon, _model(tmp_path))
    assert address.form == "dial"
    assert address.text == "4:35 and 3,000 feet from the Man"


# --- outside the city --------------------------------------------------------------------


def test_far_outside_is_miles_and_compass(tmp_path: Path) -> None:
    lat, lon = _latlon_at(305.0, 15840.0)  # 3 miles, bearing NW
    address = to_address(lat, lon, _model(tmp_path))
    assert address.form == "bearing"
    assert address.text == "3 miles northwest of the Man"


def test_just_outside_is_feet_and_compass(tmp_path: Path) -> None:
    lat, lon = _latlon_at(305.0, 9000.0)
    address = to_address(lat, lon, _model(tmp_path))
    assert address.form == "bearing"
    assert address.text == "9,000 feet northwest of the Man"


# --- degradation ladder ------------------------------------------------------------------


def test_no_streets_still_gives_the_dial_form(tmp_path: Path) -> None:
    model = _model(tmp_path, with_streets=False)
    lat, lon = _latlon_at(180.0, 3213.0)
    address = to_address(lat, lon, model)
    assert address.form == "dial"
    assert address.text == "4:30 and 3,210 feet from the Man"


def test_no_model_at_all_is_raw_but_honest(tmp_path: Path) -> None:
    # 137: no [brc] city configured ⇒ no origin to measure from — plain coordinates only.
    address = to_address(*_TUMA, None)
    assert address.form == "raw"
    assert "40.77882" in address.text
    assert "of the Man" not in address.text


def test_empty_cache_never_fetches_and_still_answers(tmp_path: Path) -> None:
    cache = tmp_path / "empty"
    cache.mkdir()

    def _quiet_fetcher(name: str) -> bytes | None:
        return None  # unavailable, never raises — the BrcSource contract

    model = derive_city_model(BrcSource(cache, origin=ORIGIN, year=YEAR, fetcher=_quiet_fetcher))
    assert model.orientation_source == "fallback"
    lat, lon = _latlon_at(225.0, 3000.0)
    address = to_address(lat, lon, model)
    assert address.form == "dial"
    assert address.clock == "6:00"


def test_returns_are_typed(tmp_path: Path) -> None:
    address = to_address(*_TUMA, _model(tmp_path))
    assert isinstance(address, BrcAddress)


# --- named centerlines (the 2026-08-29 field follow-on: "6:15 and Route 66") -------------


def test_tuma_gets_route_66_from_the_named_centerlines(tmp_path: Path) -> None:
    # The field truth that motivated this path: the "unnameable" ring near Center Camp IS
    # a named street. With street_lines cached, TUMA's answer is the real corner.
    address = to_address(*_TUMA, _model(tmp_path, named=True))
    assert address.form == "corner"
    assert address.text == "6:15 & Route 66"
    assert address.street == "Route 66"


def test_named_corner_and_esplanade_display(tmp_path: Path) -> None:
    model = _model(tmp_path, named=True)
    lat, lon = _latlon_at(180.0, 3213.0)
    assert to_address(lat, lon, model).text == "4:30 & Bravo"  # spoken: NATO phonetic
    lat, lon = _latlon_at(270.0, 2311.0)
    assert to_address(lat, lon, model).text == "7:30 & Esplanade"  # "ESP" → the spoken name


def test_rods_road_is_spoken_as_esplanade(tmp_path: Path) -> None:
    # Operator decision 2026-08-30: the survey's "Rods Road" (the Center Camp ring) is
    # called Esplanade on the air — the data stays true, only the spoken name changes.
    lat, lon = _latlon_at(225.0, 2450.0)  # dead on the circle's playa-side arc
    address = to_address(lat, lon, _model(tmp_path, named=True))
    assert address.form == "corner"
    assert address.text == "6:00 & Esplanade"
    assert address.street == "Esplanade"


def test_same_street_both_sides_is_a_corner_not_between(tmp_path: Path) -> None:
    # Tooma, 2026-08-30: playa-side of Esplanade near the keyhole, the Center Camp
    # circle's arcs bracketing its radius — "between Rods Road and Rods Road" live. A
    # same-name block collapses to the corner form; "between X and X" must never render.
    lat, lon = _latlon_at(225.0, 2240.0)
    address = to_address(lat, lon, _model(tmp_path, named=True))
    assert address.form == "corner"
    assert address.text == "6:00 & Esplanade"


def test_mid_block_names_the_block_not_the_distance(tmp_path: Path) -> None:
    lat, lon = _latlon_at(180.0, 3353.0)  # 140 ft from both B and C
    address = to_address(lat, lon, _model(tmp_path, named=True))
    assert address.form == "between"
    assert address.text == "4:30, between Bravo and Charlie"


def test_radial_avenue_is_never_a_corner(tmp_path: Path) -> None:
    # Dead on the 6:00 avenue centerline, mid-block between A and B: the clock already
    # says "6:00", so the ring block is the useful half of the address.
    lat, lon = _latlon_at(225.0, 3070.0)
    address = to_address(lat, lon, _model(tmp_path, named=True))
    assert address.form == "between"
    assert address.text == "6:00, between Alpha and Bravo"


def test_inside_esplanade_dial_carries_both_distances(tmp_path: Path) -> None:
    # 126 (operator directive 2026-09-02): between the Esplanade and the Man, say the
    # distance from each. At 6:00 the nearest Esplanade-named geometry is the Center
    # Camp keyhole road (Rods Road, spoken as Esplanade) — nearer than the 2311 ring.
    lat, lon = _latlon_at(225.0, 1500.0)
    address = to_address(lat, lon, _model(tmp_path, named=True))
    assert address.form == "dial"
    assert address.text == "6:00, 1,500 feet from the Man and 650 feet from the Esplanade"


def test_deep_playa_outside_the_city_arc_stays_plain_dial(tmp_path: Path) -> None:
    # 12:00 is outside the 2:00-10:00 arc — no Esplanade there to measure from.
    lat, lon = _latlon_at(45.0, 1500.0)
    address = to_address(lat, lon, _model(tmp_path, named=True))
    assert address.form == "dial"
    assert address.text == "12:00 and 1,500 feet from the Man"


def test_ten_oclock_boundary_is_inside_the_arc(tmp_path: Path) -> None:
    lat, lon = _latlon_at(345.0, 1500.0)  # 10:00 exactly
    address = to_address(lat, lon, _model(tmp_path, named=True))
    assert address.form == "dial"
    assert address.text.startswith("10:00, 1,500 feet from the Man and ")
    assert address.text.endswith(" feet from the Esplanade")


def test_named_far_outside_is_still_bearing(tmp_path: Path) -> None:
    lat, lon = _latlon_at(305.0, 15840.0)
    address = to_address(lat, lon, _model(tmp_path, named=True))
    assert address.form == "bearing"
    assert address.text == "3 miles northwest of the Man"
