"""Unit tests: terrain foundations + profiles + clearance + grids (feature 031).

Everything runs offline: tiles are synthesized by the test-only terrarium encoder
(tests/fixtures/ar/terrarium_helpers.py), which also lets each PNG scanline filter type
pin the production decoder's defilter paths.
"""

from __future__ import annotations

import math
from pathlib import Path

from obi.core.geo import fresnel_local_radius_m, fresnel_radius_m
from obi.core.models import (
    ArEndpoint,
    ArGeometry,
    ArLink,
    ArSite,
    ClearanceBasis,
    ClearanceVerdict,
    GroundProvenance,
    HeightProvenance,
    LinkHealth,
    LinkProfile,
    _utcnow,
)
from obi.core.terrain import (
    GRID_HALF_EXTENT_MAX_M,
    GRID_HALF_EXTENT_MIN_M,
    GRID_POSTS,
    ZOOM,
    Summit,
    TerrainTileSource,
    apply_terrain,
    build_profile,
    build_site_grid,
    decode_terrarium,
    evaluate_clearance,
    global_pixel,
    peaks_for_site,
    tile_for,
)
from tests.fixtures.ar.terrarium_helpers import (
    encode_terrarium,
    flat_tile_bytes,
    terrarium_rgb,
)

PLAYA = 1190.66
LAT, LON = 40.9, -119.1  # Black Rock Desert-ish


def flat_source(tmp_path: Path, elevation: float = 1200.0) -> TerrainTileSource:
    tile = flat_tile_bytes(elevation)
    return TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: tile)


# --- T001: encoder self round-trip ---------------------------------------------------------


def test_encoder_round_trips_through_the_production_decoder() -> None:
    grid = [[0.0, 100.25, -430.0], [8848.0, 1190.66, 1370.0], [1.0, 2.0, 3.0]]
    for filter_type in range(5):
        decoded = decode_terrarium(encode_terrarium(grid, filter_type=filter_type))
        assert decoded is not None, f"filter {filter_type}"
        for row, expected_row in zip(decoded, grid, strict=True):
            for value, expected in zip(row, expected_row, strict=True):
                assert abs(value - expected) < 1 / 256 + 1e-9, f"filter {filter_type}"


def test_terrarium_rgb_encoding_is_the_documented_formula() -> None:
    r, g, b = terrarium_rgb(0.0)
    assert (r * 256 + g + b / 256) - 32768 == 0.0


# --- T002: tile math + decode edge cases ---------------------------------------------------


def test_global_pixel_anchors() -> None:
    """Non-circular anchors: exact points of the Web Mercator definition."""

    # lon −180 is the left edge; lon 0 the middle; the Mercator limit is the top edge.
    n = (1 << ZOOM) * 256
    assert global_pixel(0.0, -180.0) == (0.0, n / 2)
    x, y = global_pixel(0.0, 0.0)
    assert (x, y) == (n / 2, n / 2)
    _x, y_top = global_pixel(85.0511287798066, 0.0)
    assert abs(y_top) < 1e-6 * n


def test_tile_for_matches_pixel_space() -> None:
    x, y = tile_for(LAT, LON)
    px, py = global_pixel(LAT, LON)
    assert x == int(px // 256)
    assert y == int(py // 256)
    assert 0 <= x < (1 << ZOOM) and 0 <= y < (1 << ZOOM)


def test_decode_rejects_non_terrarium_shapes() -> None:
    good = flat_tile_bytes(1200.0, size=8)
    assert decode_terrarium(good) is not None
    assert decode_terrarium(b"") is None
    assert decode_terrarium(b"\x89PNG\r\n\x1a\n") is None
    assert decode_terrarium(good[:40]) is None  # truncated
    assert decode_terrarium(b"junk" * 100) is None

    def patched(data: bytes, offset: int, value: int) -> bytes:
        raw = bytearray(data)
        raw[offset] = value
        return bytes(raw)

    # IHDR payload starts at byte 16: width(4) height(4) depth(1) color(1) comp filt interlace
    assert decode_terrarium(patched(good, 24, 16)) is None  # 16-bit depth
    assert decode_terrarium(patched(good, 25, 3)) is None  # paletted
    assert decode_terrarium(patched(good, 28, 1)) is None  # interlaced


def test_decode_rejects_corrupt_zlib_stream() -> None:
    good = bytearray(flat_tile_bytes(1200.0, size=8))
    # Stomp bytes in the middle of the IDAT payload.
    idat_at = bytes(good).index(b"IDAT")
    good[idat_at + 10 : idat_at + 14] = b"\x00\x00\x00\x00"
    assert decode_terrarium(bytes(good)) is None


def test_rgba_tiles_decode_too() -> None:
    grid = [[123.0, 456.5], [789.25, -12.0]]
    decoded = decode_terrarium(encode_terrarium(grid, filter_type=2, rgba=True))
    assert decoded is not None
    assert abs(decoded[1][0] - 789.25) < 1 / 256 + 1e-9


# --- T004: TerrainTileSource cache behavior ------------------------------------------------


def test_tiles_are_fetched_once_and_cached_durably(tmp_path: Path) -> None:
    calls: list[tuple[int, int, int]] = []
    tile = flat_tile_bytes(1200.0)

    def fetcher(z: int, x: int, y: int) -> bytes:
        calls.append((z, x, y))
        return tile

    source = TerrainTileSource(tmp_path / "tiles", fetcher=fetcher)
    assert source.elevation(LAT, LON) is not None
    assert source.elevation(LAT, LON) is not None  # in-memory hit
    first_calls = len(calls)
    assert first_calls >= 1
    assert source.tiles_fetched == first_calls

    # A NEW source (new process) reads from disk with no fetcher involvement.
    offline = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: None)
    value = offline.elevation(LAT, LON)
    assert value is not None and abs(value - 1200.0) < 0.01
    assert offline.tiles_fetched == 0
    assert offline.tiles_cached >= 1


def test_unfetchable_tile_is_no_data(tmp_path: Path) -> None:
    source = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: None)
    assert source.elevation(LAT, LON) is None


def test_corrupt_cached_tile_self_heals(tmp_path: Path) -> None:
    good = flat_tile_bytes(1234.0)
    source = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: good)
    x, y = tile_for(LAT, LON)
    path = tmp_path / "tiles" / str(ZOOM) / str(x) / f"{y}.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a png at all")

    value = source.elevation(LAT, LON)
    assert value is not None and abs(value - 1234.0) < 0.01
    assert decode_terrarium(path.read_bytes()) is not None  # cache replaced with good bytes


def test_undecodable_fetch_is_not_cached(tmp_path: Path) -> None:
    source = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: b"garbage")
    assert source.elevation(LAT, LON) is None
    assert not list((tmp_path / "tiles").rglob("*.png"))


def test_implausible_elevations_are_no_data(tmp_path: Path) -> None:
    assert flat_source(tmp_path, 20000.0).elevation(LAT, LON) is None


def test_bilinear_sampling_is_smooth_and_bounded(tmp_path: Path) -> None:
    """A west→east gradient tile must sample monotonically between pixel values."""

    gradient = [[float(col) for col in range(256)] for _row in range(256)]
    tile = encode_terrarium(gradient)
    source = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: tile)
    step = 360.0 / (1 << ZOOM) / 256  # one pixel of longitude at the equator
    values = [source.elevation(0.0, LON + i * step / 4) for i in range(9)]
    assert all(v is not None for v in values)
    floats = [v for v in values if v is not None]
    assert all(b >= a - 1e-9 for a, b in zip(floats, floats[1:], strict=False))
    assert all(0.0 <= v <= 255.0 for v in floats)


# --- T006/T007: profiles + datum anchoring -------------------------------------------------


def test_profile_ends_equal_the_resolving_grounds(tmp_path: Path) -> None:
    source = flat_source(tmp_path, 1200.0)
    profile = build_profile(
        source,
        lat_a=LAT,
        lon_a=LON,
        ground_a=PLAYA,
        lat_b=LAT + 0.02,
        lon_b=LON + 0.02,
        ground_b=1370.0,
        length_m=3000.0,
    )
    assert profile is not None
    assert profile.samples_amsl[0] == round(PLAYA, 1)
    assert profile.samples_amsl[-1] == 1370.0
    # DEM is flat, so the anchored profile is exactly the linear blend of the corrections.
    count = len(profile.samples_amsl)
    assert count == max(3, math.ceil(3000.0 / 30.0) + 1)
    mid = profile.samples_amsl[(count - 1) // 2]
    assert abs(mid - (PLAYA + 1370.0) / 2) < 1.0


def test_constant_datum_offset_cancels_exactly(tmp_path: Path) -> None:
    # DEM says 1200 everywhere; both grounds say 1190 — a constant −10 datum offset.
    profile = build_profile(
        flat_source(tmp_path, 1200.0),
        lat_a=LAT,
        lon_a=LON,
        ground_a=1190.0,
        lat_b=LAT + 0.01,
        lon_b=LON,
        ground_b=1190.0,
        length_m=1100.0,
    )
    assert profile is not None
    assert all(sample == 1190.0 for sample in profile.samples_amsl)


def test_short_link_still_gets_three_samples(tmp_path: Path) -> None:
    profile = build_profile(
        flat_source(tmp_path),
        lat_a=LAT,
        lon_a=LON,
        ground_a=PLAYA,
        lat_b=LAT + 0.0001,
        lon_b=LON,
        ground_b=PLAYA,
        length_m=11.0,
    )
    assert profile is not None
    assert len(profile.samples_amsl) == 3


def test_any_missing_sample_omits_the_whole_profile(tmp_path: Path) -> None:
    """No interpolation across holes: a path leaving the covered tile yields None."""

    home = tile_for(LAT, LON)
    tile = flat_tile_bytes(1200.0)

    def one_tile_only(z: int, x: int, y: int) -> bytes | None:
        return tile if (x, y) == home else None

    source = TerrainTileSource(tmp_path / "tiles", fetcher=one_tile_only)
    profile = build_profile(
        source,
        lat_a=LAT,
        lon_a=LON,
        ground_a=PLAYA,
        lat_b=LAT,
        lon_b=LON + 0.5,  # ~5 tiles east at z12
        ground_b=PLAYA,
        length_m=42000.0,
    )
    assert profile is None


# --- T006: clearance verdicts (synthetic ridge, SC-003) -------------------------------------


def _ridge_profile(
    count: int, ground: float, *, ridge_at: float, ridge_height: float
) -> LinkProfile:
    samples = [ground] * count
    samples[round(ridge_at * (count - 1))] = ground + ridge_height
    return LinkProfile(samples_amsl=[round(s, 1) for s in samples])


def test_local_fresnel_radius_matches_the_025_midspan_formula() -> None:
    assert math.isclose(
        fresnel_local_radius_m(500.0, 500.0, 1000.0, 5500.0),
        fresnel_radius_m(1000.0, 5500.0),
        rel_tol=1e-12,
    )
    assert fresnel_local_radius_m(0.0, 1000.0, 1000.0, 5500.0) == 0.0


def test_flat_path_is_clear() -> None:
    clearance = evaluate_clearance(
        LinkProfile(samples_amsl=[1190.0] * 34),
        height_a_amsl=1200.0,
        height_b_amsl=1200.0,
        length_m=1000.0,
        frequency_ghz=5.5,
    )
    assert clearance.verdict is ClearanceVerdict.CLEAR
    assert clearance.basis is ClearanceBasis.FRESNEL
    assert clearance.margin_m > 0


def test_ridge_inside_full_zone_is_marginal_and_located() -> None:
    # D=1000 m, f=5.5 GHz: at t=0.4 the local F1 radius is ~3.62 m (0.6 core ~2.17 m).
    # A ridge leaving 3.0 m of clearance sits inside F1 but outside the core.
    count = 51  # ridge lands exactly on a sample (t=0.4 → index 20)
    line = 1200.0
    profile = _ridge_profile(count, 1150.0, ridge_at=0.4, ridge_height=line - 3.0 - 1150.0)
    clearance = evaluate_clearance(
        profile,
        height_a_amsl=line,
        height_b_amsl=line,
        length_m=1000.0,
        frequency_ghz=5.5,
    )
    assert clearance.verdict is ClearanceVerdict.MARGINAL
    assert abs(clearance.at_fraction - 0.4) <= 1.0 / (count - 1)  # within one sample step
    assert clearance.margin_m > 0


def test_ridge_inside_the_core_is_obstructed() -> None:
    count = 51
    line = 1200.0
    profile = _ridge_profile(count, 1150.0, ridge_at=0.4, ridge_height=line - 1.0 - 1150.0)
    clearance = evaluate_clearance(
        profile,
        height_a_amsl=line,
        height_b_amsl=line,
        length_m=1000.0,
        frequency_ghz=5.5,
    )
    assert clearance.verdict is ClearanceVerdict.OBSTRUCTED
    assert clearance.margin_m < 0
    assert abs(clearance.at_fraction - 0.4) <= 1.0 / (count - 1)


def test_no_frequency_means_los_basis_two_state() -> None:
    below = evaluate_clearance(
        LinkProfile(samples_amsl=[1190.0] * 11),
        height_a_amsl=1200.0,
        height_b_amsl=1200.0,
        length_m=500.0,
        frequency_ghz=None,
    )
    assert below.basis is ClearanceBasis.LOS
    assert below.verdict is ClearanceVerdict.CLEAR

    blocked = evaluate_clearance(
        _ridge_profile(11, 1190.0, ridge_at=0.5, ridge_height=15.0),
        height_a_amsl=1200.0,
        height_b_amsl=1200.0,
        length_m=500.0,
        frequency_ghz=None,
    )
    assert blocked.verdict is ClearanceVerdict.OBSTRUCTED
    assert blocked.basis is ClearanceBasis.LOS


def test_earth_curvature_bulge_is_applied() -> None:
    """A 20 km grazing path clears flat terrain by 5 m — less than the ~5.9 m 4/3-earth
    mid-path bulge — so it must be judged obstructed even though LOS never meets ground."""

    clearance = evaluate_clearance(
        LinkProfile(samples_amsl=[1190.0] * 668),
        height_a_amsl=1195.0,
        height_b_amsl=1195.0,
        length_m=20000.0,
        frequency_ghz=None,
    )
    assert clearance.verdict is ClearanceVerdict.OBSTRUCTED
    assert abs(clearance.at_fraction - 0.5) < 0.01


def test_verdict_never_touches_health() -> None:
    link = ArLink(
        id="l1",
        name="a <-> b (5 GHz)",
        a="a-ptp-b-05",
        b="b-ptp-a-05",
        band="5 GHz",
        health=LinkHealth.HEALTHY,
        signal_dbm=-60.0,
        length_m=1000.0,
        frequency_ghz=5.5,
    )
    link.clearance = evaluate_clearance(
        _ridge_profile(11, 1190.0, ridge_at=0.5, ridge_height=50.0),
        height_a_amsl=1200.0,
        height_b_amsl=1200.0,
        length_m=link.length_m,
        frequency_ghz=link.frequency_ghz,
    )
    assert link.clearance.verdict is ClearanceVerdict.OBSTRUCTED
    assert link.health is LinkHealth.HEALTHY  # FR-006: advisory only


# --- T016: site grids -----------------------------------------------------------------------


def test_flat_grid_is_all_zero_and_sized(tmp_path: Path) -> None:
    grid = build_site_grid(
        flat_source(tmp_path, 1200.0), latitude=LAT, longitude=LON, enu={}
    )
    assert grid is not None
    assert grid.posts == GRID_POSTS
    assert grid.half_extent_m == GRID_HALF_EXTENT_MIN_M  # empty ENU → floor
    assert len(grid.rel_heights_dm) == GRID_POSTS * GRID_POSTS
    assert set(grid.rel_heights_dm) == {0}  # self-referenced datum: flat DEM ⇒ all zero


def test_grid_extent_tracks_endpoints_and_clamps(tmp_path: Path) -> None:
    near = build_site_grid(
        flat_source(tmp_path),
        latitude=LAT,
        longitude=LON,
        enu={"x": (5000.0, 0.0, 10.0)},
    )
    far = build_site_grid(
        flat_source(tmp_path),
        latitude=LAT,
        longitude=LON,
        enu={"x": (30000.0, 0.0, 10.0)},
    )
    assert near is not None and near.half_extent_m == 7000.0  # reach + 2 km margin
    assert far is not None and far.half_extent_m == GRID_HALF_EXTENT_MAX_M  # clamped


def test_grid_rows_run_south_to_north_and_west_to_east(tmp_path: Path) -> None:
    """Pin the row-major orientation with a DEM that rises northward and eastward."""

    ref_x, ref_y = global_pixel(LAT, LON)

    def tile_bytes(z: int, x: int, y: int) -> bytes:
        grid = [
            [
                1000.0
                + (ref_y - (y * 256 + row)) * 0.05  # north (smaller gy) = higher
                + ((x * 256 + col) - ref_x) * 0.02  # east = higher
                for col in range(256)
            ]
            for row in range(256)
        ]
        return encode_terrarium(grid)

    source = TerrainTileSource(tmp_path / "tiles", fetcher=tile_bytes)
    grid = build_site_grid(source, latitude=LAT, longitude=LON, enu={})
    assert grid is not None
    posts = grid.posts
    first_row_mid = grid.rel_heights_dm[posts // 2]
    last_row_mid = grid.rel_heights_dm[(posts - 1) * posts + posts // 2]
    assert last_row_mid > first_row_mid  # later rows are farther north = higher
    mid_row = (posts // 2) * posts
    assert grid.rel_heights_dm[mid_row + posts - 1] > grid.rel_heights_dm[mid_row]  # east


def test_grid_with_missing_tile_is_omitted_whole(tmp_path: Path) -> None:
    home = tile_for(LAT, LON)
    tile = flat_tile_bytes(1200.0)
    source = TerrainTileSource(
        tmp_path / "tiles", fetcher=lambda z, x, y: tile if (x, y) == home else None
    )
    assert build_site_grid(source, latitude=LAT, longitude=LON, enu={}) is None


# --- T020-adjacent: peaks placement (pure math; GNIS parsing is test_gnis.py) ---------------


def test_peaks_inside_extent_get_enu_and_dem_heights(tmp_path: Path) -> None:
    source = flat_source(tmp_path, 1200.0)
    summits = [
        Summit(name="Near Peak", latitude=LAT + 0.01, longitude=LON),  # ~1.1 km north
        Summit(name="Far Peak", latitude=LAT + 1.0, longitude=LON),  # ~111 km north
    ]
    peaks = peaks_for_site(
        source, summits, latitude=LAT, longitude=LON, half_extent_m=2000.0
    )
    assert [peak.name for peak in peaks] == ["Near Peak"]
    east, north, up = peaks[0].enu
    assert abs(east) < 1.0
    assert 1000.0 < north < 1200.0
    assert up == 0.0  # flat DEM, self-referenced


# --- apply_terrain orchestration ------------------------------------------------------------


def _tiny_geometry() -> ArGeometry:
    site = ArSite(
        name="Playa",
        latitude=LAT,
        longitude=LON,
        ground_elevation_m=PLAYA,
        ground_provenance=GroundProvenance.CACHE,
        enu={
            "a-ptp-b-05": (0.0, 0.0, 10.0),
            "b-ptp-a-05": (1113.2, 0.0, 10.0),
        },
    )
    endpoints = [
        ArEndpoint(
            name="a-ptp-b-05",
            site="Playa",
            latitude=LAT,
            longitude=LON,
            raw_altitude=None,
            height_amsl_m=PLAYA + 10.0,
            height_provenance=HeightProvenance.DEFAULT_MAST,
            status="active",
        ),
        ArEndpoint(
            name="b-ptp-a-05",
            site="Playa",
            latitude=LAT,
            longitude=LON + 0.01323,
            raw_altitude=None,
            height_amsl_m=PLAYA + 10.0,
            height_provenance=HeightProvenance.DEFAULT_MAST,
            status="active",
        ),
    ]
    link = ArLink(
        id="link-1",
        name="Playa <-> Playa (5 GHz)",
        a="a-ptp-b-05",
        b="b-ptp-a-05",
        band="5 GHz",
        health=LinkHealth.HEALTHY,
        signal_dbm=-55.0,
        length_m=1113.2,
        frequency_ghz=5.5,
    )
    return ArGeometry(
        generated_at=_utcnow(),
        controller="https://uisp.example.com",
        sites=[site],
        endpoints=endpoints,
        links=[link],
    )


def test_apply_terrain_fills_profiles_verdicts_and_grids(tmp_path: Path) -> None:
    geometry = _tiny_geometry()
    document = apply_terrain(geometry, tiles=flat_source(tmp_path), summits_by_state=None)
    assert set(document.profiles) == {"link-1"}
    assert geometry.links[0].clearance is not None
    assert geometry.links[0].clearance.verdict is ClearanceVerdict.CLEAR
    assert set(document.sites) == {"Playa"}
    # Peaks unconfigured ⇒ exactly one omission naming it (FR-002).
    assert [o.item for o in geometry.terrain_omitted] == ["peaks"]
    assert "gnis_states" in geometry.terrain_omitted[0].reason


def test_apply_terrain_names_per_link_and_per_site_omissions(tmp_path: Path) -> None:
    geometry = _tiny_geometry()
    none_source = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: None)
    document = apply_terrain(
        geometry, tiles=none_source, summits_by_state={"NV": [Summit("P", LAT, LON)]}
    )
    assert not document.profiles and not document.sites
    assert geometry.links[0].clearance is None
    items = [o.item for o in geometry.terrain_omitted]
    assert geometry.links[0].name in items
    assert "site Playa" in items
    assert all(o.reason for o in geometry.terrain_omitted)


def test_apply_terrain_reports_unavailable_gazetteer_states(tmp_path: Path) -> None:
    geometry = _tiny_geometry()
    apply_terrain(geometry, tiles=flat_source(tmp_path), summits_by_state={"NV": None})
    peaks_omissions = [o for o in geometry.terrain_omitted if o.item == "peaks"]
    assert len(peaks_omissions) == 1
    assert "NV" in peaks_omissions[0].reason


def test_apply_terrain_places_configured_peaks(tmp_path: Path) -> None:
    geometry = _tiny_geometry()
    document = apply_terrain(
        geometry,
        tiles=flat_source(tmp_path),
        summits_by_state={"NV": [Summit(name="Old Razorback", latitude=LAT, longitude=LON)]},
    )
    assert [peak.name for peak in document.sites["Playa"].peaks] == ["Old Razorback"]
    assert not [o for o in geometry.terrain_omitted if o.item == "peaks"]


# --- T024: the real-world tile format (recorded fixture, EPQS-fixture precedent) ------------


def test_recorded_real_tile_decodes_to_playa_elevations() -> None:
    """One real terrarium tile (12/692/1537, Black Rock Desert, recorded 2026-07-06) pins
    the production decoder against the actual AWS-served format: the playa floor must read
    ~1190 m across the tile."""

    fixture = Path(__file__).parents[1] / "fixtures" / "ar" / "terrarium" / "12-692-1537.png"
    grid = decode_terrarium(fixture.read_bytes())
    assert grid is not None
    assert len(grid) == 256 and len(grid[0]) == 256
    values = [value for row in grid for value in row]
    assert min(values) > 1150.0 and max(values) < 1300.0  # the playa, not garbage


def test_endpoint_samples_render_but_never_judge() -> None:
    """Live regression (2026-07-06): a healthy 60 GHz link read "obstructed, −0.3 m @ 0%"
    because its 022-resolved radio height sat 0.3 m below its own site ground. Endpoint
    samples are not obstruction candidates — the interior is what the verdict scans; the
    near-mast grazing that remains lands in the marginal band (the 0.6 core stays clear),
    never a false OBSTRUCTED (SC-003)."""

    ground = 1370.0
    clearance = evaluate_clearance(
        LinkProfile(samples_amsl=[ground] * 21),
        height_a_amsl=ground - 0.3,  # the AMSL-provenance quirk: just below "its" ground
        height_b_amsl=ground + 10.0,
        length_m=500.0,
        frequency_ghz=60.0,
    )
    assert clearance.verdict is not ClearanceVerdict.OBSTRUCTED
    assert clearance.margin_m >= 0.0
    assert 0.0 < clearance.at_fraction < 1.0
