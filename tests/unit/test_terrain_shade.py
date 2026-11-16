"""Unit tests: terrain hillshade underlay — shader, encoder, registration, policy (055).

Everything runs offline: elevation grids are hand-built or synthesized by the test-only
terrarium encoder through the injectable tile fetcher (the 031 discipline). All numeric
decisions live in core; these tests pin them.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from obi.core.geo import enu_offset
from obi.core.models import ArGeometry, ArSite, GroundProvenance, _utcnow
from obi.core.terrain import (
    GRID_MARGIN_M,
    SHADE_PARAMS_VERSION,
    SHADE_STEP_M,
    ShadeMetaFile,
    TerrainTileSource,
    apply_terrain_shade,
    decode_terrarium,
    encode_shade_png,
    hillshade_grid,
    plan_shade,
    read_shade_meta,
    sample_shade_elevations,
    shade_covers,
    shade_registration,
)
from tests.fixtures.ar.terrarium_helpers import flat_tile_bytes

LAT, LON = 40.9, -119.1


def flat_source(tmp_path: Path, elevation: float = 1200.0) -> TerrainTileSource:
    tile = flat_tile_bytes(elevation)
    return TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: tile)


def _sites(*positions: tuple[str, float, float]) -> ArGeometry:
    return ArGeometry(
        generated_at=_utcnow(),
        controller="https://uisp.example.com",
        sites=[
            ArSite(
                name=name,
                latitude=lat,
                longitude=lon,
                ground_elevation_m=1190.66,
                ground_provenance=GroundProvenance.CACHE,
            )
            for name, lat, lon in positions
        ],
        endpoints=[],
        links=[],
    )


# --- plan_shade: bbox, origin, pixel alignment ------------------------------------------------


def test_plan_shade_covers_every_site_plus_margin() -> None:
    plan = plan_shade([(LAT, LON, GRID_MARGIN_M), (LAT + 0.02, LON + 0.02, GRID_MARGIN_M)])
    assert plan is not None
    # origin is the bbox center
    assert abs(plan.origin_lat - (LAT + 0.01)) < 1e-9
    assert abs(plan.origin_lon - (LON + 0.01)) < 1e-9
    # both sites, expanded by the margin, fall inside the rectangle
    for lat, lon in ((LAT, LON), (LAT + 0.02, LON + 0.02)):
        east, north = enu_offset(plan.origin_lat, plan.origin_lon, lat, lon)
        assert plan.e_min <= east - GRID_MARGIN_M
        assert east + GRID_MARGIN_M <= plan.e_max
        assert plan.n_min <= north - GRID_MARGIN_M
        assert north + GRID_MARGIN_M <= plan.n_max
    # whole-pixel alignment
    assert abs((plan.e_max - plan.e_min) - plan.width * plan.step_m) < 1e-6
    assert abs((plan.n_max - plan.n_min) - plan.height * plan.step_m) < 1e-6


def test_plan_shade_covers_each_sites_wireframe_square() -> None:
    """055 follow-up: the underlay must reach as far as the 3D wireframe for ANY anchor —
    each site's own grid square (site ± its half-extent), not just the site bbox + margin."""

    extents = [(LAT, LON, 21000.0), (LAT + 0.13, LON - 0.02, 28000.0)]
    plan = plan_shade(extents)
    assert plan is not None
    for lat, lon, half in extents:
        east, north = enu_offset(plan.origin_lat, plan.origin_lon, lat, lon)
        assert plan.e_min <= east - half
        assert east + half <= plan.e_max
        assert plan.n_min <= north - half
        assert north + half <= plan.n_max


def test_plan_shade_empty_is_none() -> None:
    assert plan_shade([]) is None


def test_plan_shade_clamps_a_far_outlier_site() -> None:
    """Principle VI: one far-flung / mis-located site must not explode the raster. The bbox
    stays bounded by the centroid clamp, and the outlier falls outside the covered rectangle."""

    from obi.core.terrain import SHADE_MAX_HALF_EXTENT_M

    cluster = [(LAT + i * 0.001, LON + i * 0.001, GRID_MARGIN_M) for i in range(37)]
    # ~278 km north — a stray mis-located site whose own grid clamps at the 28 km cap
    outlier = (LAT + 2.5, LON, SHADE_MAX_HALF_EXTENT_M)
    plan = plan_shade(cluster + [outlier])
    assert plan is not None
    limit = 4.0 * SHADE_MAX_HALF_EXTENT_M + 2.0 * plan.step_m  # ±(cap + cap), pixel-grown
    assert (plan.e_max - plan.e_min) <= limit
    assert (plan.n_max - plan.n_min) <= limit  # bounded, not continental

    named = {f"c{i}": (p[0], p[1]) for i, p in enumerate(cluster)}
    named["outlier"] = (outlier[0], outlier[1])
    rects = shade_registration(
        named,
        origin_lat=plan.origin_lat,
        origin_lon=plan.origin_lon,
        bbox_enu=plan.bbox_enu,
    )
    assert "outlier" not in rects  # the far site gets no underlay rect (US4-6)
    assert sum(1 for k in rects if k.startswith("c")) >= 30  # the cluster is covered


# --- sampling: flat everywhere / no-data refuses to guess -------------------------------------


def test_sample_flat_tiles_is_uniform(tmp_path: Path) -> None:
    plan = plan_shade([(LAT, LON, GRID_MARGIN_M), (LAT + 0.005, LON + 0.005, GRID_MARGIN_M)])
    assert plan is not None
    grid = sample_shade_elevations(flat_source(tmp_path, 1200.0), plan)
    assert grid is not None
    assert len(grid) == plan.height and all(len(row) == plan.width for row in grid)
    assert all(abs(v - 1200.0) < 0.01 for row in grid for v in row)


def test_sample_missing_tile_aborts_to_none(tmp_path: Path) -> None:
    plan = plan_shade([(LAT, LON, GRID_MARGIN_M), (LAT + 0.005, LON + 0.005, GRID_MARGIN_M)])
    assert plan is not None
    none_source = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: None)
    assert sample_shade_elevations(none_source, plan) is None


# --- Horn hillshade ---------------------------------------------------------------------------


def test_flat_terrain_shades_to_the_standard_flat_value() -> None:
    grid = hillshade_grid([[1200.0] * 5 for _ in range(5)], SHADE_STEP_M)
    # 45° altitude ⇒ 255·cos(45°) ≈ 180
    assert all(abs(v - 180) <= 1 for row in grid for v in row)


def test_planar_ramp_shades_uniformly() -> None:
    # A constant east-west tilt ⇒ constant slope + aspect ⇒ one interior value everywhere.
    ramp = [[float(col) * 3.0 for col in range(9)] for _row in range(9)]
    grid = hillshade_grid(ramp, SHADE_STEP_M)
    interior = {grid[r][c] for r in range(1, 8) for c in range(1, 8)}
    assert len(interior) == 1  # uniform


def test_cone_is_bright_northwest_and_dark_southeast() -> None:
    # A peak at the center: the NW flank faces the 315° light (bright), the SE flank away.
    n = 21
    center = (n - 1) / 2.0
    cone = [
        [200.0 - math.hypot(r - center, c - center) * 5.0 for c in range(n)] for r in range(n)
    ]
    grid = hillshade_grid(cone, SHADE_STEP_M)
    q = n // 4
    nw = grid[q][q]  # north-west (small row, small col)
    se = grid[n - 1 - q][n - 1 - q]  # south-east
    assert nw > se
    assert nw > 180 and se < 180  # lit vs shadowed relative to the flat value


def test_hillshade_values_stay_in_byte_range() -> None:
    cone = [[100.0 - math.hypot(r - 5, c - 5) * 40.0 for c in range(11)] for r in range(11)]
    grid = hillshade_grid(cone, SHADE_STEP_M)
    assert all(0 <= v <= 255 for row in grid for v in row)


# --- PNG encoder round-trips through the production terrarium decoder (research R3) ------------


def _gray_from_terrarium(decoded: float) -> int:
    # r=g=b=v ⇒ decoded = v·256 + v + v/256 − 32768 = v·65793/256 − 32768
    return round((decoded + 32768.0) * 256.0 / 65793.0)


def test_encode_shade_png_round_trips_gray_through_decode_terrarium() -> None:
    grid = [[0, 45, 128], [180, 255, 7], [200, 199, 1]]
    decoded = decode_terrarium(encode_shade_png(grid))
    assert decoded is not None
    for out_row, in_row in zip(decoded, grid, strict=True):
        for value, expected in zip(out_row, in_row, strict=True):
            assert _gray_from_terrarium(value) == expected


def test_encode_shade_png_is_a_valid_truecolor_png() -> None:
    png = encode_shade_png([[10, 20], [30, 40]])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert decode_terrarium(png) is not None  # non-interlaced 8-bit truecolor


# --- registration: rects agree with the ENU table within one post (SC-004) --------------------


def test_registration_rects_agree_with_enu_offsets() -> None:
    a = (LAT, LON)
    b = (LAT + 0.02, LON - 0.01)
    plan = plan_shade([(*a, GRID_MARGIN_M), (*b, GRID_MARGIN_M)])
    assert plan is not None
    rects = shade_registration(
        {"A": a, "B": b},
        origin_lat=plan.origin_lat,
        origin_lon=plan.origin_lon,
        bbox_enu=plan.bbox_enu,
    )
    assert set(rects) == {"A", "B"}
    # A world point at B's location, placed via A's rect, must match A.enu[B] within one post.
    be, bn = enu_offset(plan.origin_lat, plan.origin_lon, *b)  # B in the origin frame
    a_rect = rects["A"]  # (e_min, n_min, e_max, n_max) in A's frame
    # the rect's west/south edge in A's frame corresponds to origin-frame (e_min, n_min);
    # so origin-frame point (be, bn) sits at A-frame (a_rect.e_min + (be - plan.e_min), …)
    placed_e = a_rect[0] + (be - plan.e_min)
    placed_n = a_rect[1] + (bn - plan.n_min)
    expected_e, expected_n = enu_offset(*a, *b)
    assert abs(placed_e - expected_e) < SHADE_STEP_M
    assert abs(placed_n - expected_n) < SHADE_STEP_M


def test_registration_omits_sites_outside_the_raster() -> None:
    a = (LAT, LON)
    plan = plan_shade([(*a, GRID_MARGIN_M)])
    assert plan is not None
    far = (LAT + 5.0, LON)  # hundreds of km away — well outside the bbox
    rects = shade_registration(
        {"A": a, "Far": far},
        origin_lat=plan.origin_lat,
        origin_lon=plan.origin_lon,
        bbox_enu=plan.bbox_enu,
    )
    assert set(rects) == {"A"}  # US4-6: uncovered anchor gets no rect (no misregistration)


# --- reuse / regenerate policy ----------------------------------------------------------------


def test_shade_covers_true_within_and_false_beyond() -> None:
    meta = ShadeMetaFile(
        params_version=SHADE_PARAMS_VERSION,
        origin_lat=LAT,
        origin_lon=LON,
        step_m=SHADE_STEP_M,
        width=100,
        height=100,
        bbox_enu=(-6000.0, -6000.0, 6000.0, 6000.0),
        generated_at=_utcnow(),
    )
    assert shade_covers(meta, [(LAT, LON, GRID_MARGIN_M)])  # centered, its square fits
    # ~5.5 km north — the site's square escapes the bbox
    assert not shade_covers(meta, [(LAT + 0.05, LON, GRID_MARGIN_M)])
    # centered, but a wireframe-scale half-extent outgrows the 6 km raster (055 follow-up)
    assert not shade_covers(meta, [(LAT, LON, 21000.0)])


def test_read_shade_meta_round_trips(tmp_path: Path) -> None:
    geometry = _sites(("A", LAT, LON), ("B", LAT + 0.01, LON + 0.01))
    png = tmp_path / "terrain-shade.png"
    meta = tmp_path / "terrain-shade.meta.json"
    apply_terrain_shade(geometry, tiles=flat_source(tmp_path), png_path=png, meta_path=meta)
    parsed = read_shade_meta(meta)
    assert parsed is not None
    assert parsed.params_version == SHADE_PARAMS_VERSION
    assert parsed.width > 0 and parsed.height > 0


def test_read_shade_meta_bad_input_is_none(tmp_path: Path) -> None:
    assert read_shade_meta(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_shade_meta(bad) is None
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"params_version": 1}), encoding="utf-8")
    assert read_shade_meta(partial) is None


def test_generate_then_reuse_keeps_bytes_and_stamp(tmp_path: Path) -> None:
    geometry = _sites(("A", LAT, LON), ("B", LAT + 0.01, LON + 0.01))
    png = tmp_path / "terrain-shade.png"
    meta = tmp_path / "terrain-shade.meta.json"
    source = flat_source(tmp_path)

    m1, status1, reason1 = apply_terrain_shade(
        geometry, tiles=source, png_path=png, meta_path=meta
    )
    assert status1 == "generated" and reason1 is None and m1 is not None
    assert png.is_file() and meta.is_file()
    png_bytes = png.read_bytes()
    assert set(m1.sites) == {"A", "B"}

    m2, status2, reason2 = apply_terrain_shade(
        geometry, tiles=source, png_path=png, meta_path=meta
    )
    assert status2 == "reused" and reason2 is None and m2 is not None
    assert png.read_bytes() == png_bytes  # raster untouched on reuse
    assert m2.generated_at == m1.generated_at  # reuse keeps the original stamp


def test_apply_plans_from_the_sites_grid_half_extents(tmp_path: Path) -> None:
    """055 follow-up: the raster covers each site's 031 wireframe square, derived from the
    site's own ENU table — not just the site positions + fixed margin."""

    geometry = _sites(("A", LAT, LON))
    # a 2 km-away endpoint ⇒ the site's grid half-extent is reach + margin = 4 km
    geometry.sites[0].enu = {"radio": (2000.0, 0.0, 10.0)}
    png = tmp_path / "terrain-shade.png"
    meta_path = tmp_path / "terrain-shade.meta.json"
    m, status, _ = apply_terrain_shade(
        geometry, tiles=flat_source(tmp_path), png_path=png, meta_path=meta_path
    )
    assert status == "generated" and m is not None
    meta = read_shade_meta(meta_path)
    assert meta is not None
    e_min, n_min, e_max, n_max = meta.bbox_enu
    half = 2000.0 + GRID_MARGIN_M  # what build_site_grid uses for this site
    assert e_min <= -half and e_max >= half
    assert n_min <= -half and n_max >= half


def test_reuse_rebuilds_rect_table_for_a_new_covered_site(tmp_path: Path) -> None:
    png = tmp_path / "terrain-shade.png"
    meta = tmp_path / "terrain-shade.meta.json"
    source = flat_source(tmp_path)
    apply_terrain_shade(
        _sites(("A", LAT, LON), ("B", LAT + 0.01, LON + 0.01)),
        tiles=source,
        png_path=png,
        meta_path=meta,
    )
    # a THIRD nearby site added; the raster still covers it, so we reuse but re-register.
    m, status, _ = apply_terrain_shade(
        _sites(("A", LAT, LON), ("B", LAT + 0.01, LON + 0.01), ("C", LAT + 0.005, LON)),
        tiles=source,
        png_path=png,
        meta_path=meta,
    )
    assert status == "reused" and m is not None
    assert set(m.sites) == {"A", "B", "C"}  # fresh table, no raster work


def test_params_version_bump_forces_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import obi.core.terrain as terrain_mod

    geometry = _sites(("A", LAT, LON), ("B", LAT + 0.01, LON + 0.01))
    png = tmp_path / "terrain-shade.png"
    meta = tmp_path / "terrain-shade.meta.json"
    source = flat_source(tmp_path)
    apply_terrain_shade(geometry, tiles=source, png_path=png, meta_path=meta)

    # A shader/encoder change bumps the version ⇒ the stored raster is stale ⇒ regenerate.
    monkeypatch.setattr(terrain_mod, "SHADE_PARAMS_VERSION", SHADE_PARAMS_VERSION + 1)
    _m, status, _reason = apply_terrain_shade(
        geometry, tiles=source, png_path=png, meta_path=meta
    )
    assert status == "generated"


def test_missing_png_forces_regeneration(tmp_path: Path) -> None:
    geometry = _sites(("A", LAT, LON), ("B", LAT + 0.01, LON + 0.01))
    png = tmp_path / "terrain-shade.png"
    meta = tmp_path / "terrain-shade.meta.json"
    source = flat_source(tmp_path)
    apply_terrain_shade(geometry, tiles=source, png_path=png, meta_path=meta)
    png.unlink()  # meta present but raster gone ⇒ cannot reuse
    _m, status, _reason = apply_terrain_shade(
        geometry, tiles=source, png_path=png, meta_path=meta
    )
    assert status == "generated" and png.is_file()


def test_cold_cache_with_no_prior_omits(tmp_path: Path) -> None:
    geometry = _sites(("A", LAT, LON), ("B", LAT + 0.01, LON + 0.01))
    dead = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: None)
    m, status, reason = apply_terrain_shade(
        geometry,
        tiles=dead,
        png_path=tmp_path / "terrain-shade.png",
        meta_path=tmp_path / "terrain-shade.meta.json",
    )
    assert m is None and status == "omitted" and reason is not None
    assert not (tmp_path / "terrain-shade.png").exists()


def test_regen_failure_with_prior_keeps_raster_and_re_registers(tmp_path: Path) -> None:
    """When regeneration is forced (a new far site escapes the bbox) but the tiles are gone,
    the prior raster is kept and still registers the sites it covers, plus a named omission."""

    png = tmp_path / "terrain-shade.png"
    meta = tmp_path / "terrain-shade.meta.json"
    # first: a good raster covering A and B
    apply_terrain_shade(
        _sites(("A", LAT, LON), ("B", LAT + 0.01, LON + 0.01)),
        tiles=flat_source(tmp_path),
        png_path=png,
        meta_path=meta,
    )
    before = png.read_bytes()
    # now a far site forces regeneration, but the tiles are dead ⇒ keep prior + omit
    dead = TerrainTileSource(tmp_path / "no-tiles", fetcher=lambda z, x, y: None)
    m, status, reason = apply_terrain_shade(
        _sites(("A", LAT, LON), ("B", LAT + 0.01, LON + 0.01), ("Far", LAT + 5.0, LON)),
        tiles=dead,
        png_path=png,
        meta_path=meta,
    )
    assert status == "omitted" and reason is not None and m is not None
    assert png.read_bytes() == before  # prior raster intact
    assert set(m.sites) == {"A", "B"}  # covered sites still registered; Far omitted


def test_no_sites_omits(tmp_path: Path) -> None:
    m, status, reason = apply_terrain_shade(
        _sites(),
        tiles=flat_source(tmp_path),
        png_path=tmp_path / "terrain-shade.png",
        meta_path=tmp_path / "terrain-shade.meta.json",
    )
    assert m is None and status == "omitted" and reason is not None
