"""Contract tests: the terrain artifact (feature 031 — contracts/terrain.schema.json).

One module-scoped export against the recorded fleet fixture + synthetic flat tiles keeps
this suite fast: grids are the expensive part and only need building once.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import jsonschema
import pytest

from obi.core.audit import AuditTrail
from obi.core.config import WebSettings
from obi.core.engine import ar_export, terrain_path_for
from obi.core.gnis import GnisSource
from obi.core.models import ArGeometry, GroundProvenance
from obi.core.terrain import TerrainTileSource
from tests.fakes import geometry_snapshot
from tests.fixtures.ar.terrarium_helpers import flat_tile_bytes
from tests.unit.test_gnis import fixture_zip

SCHEMA_PATH = (
    Path(__file__).parents[2] / "tests" / "schemas" / "terrain.schema.json"
)


@pytest.fixture(scope="module", autouse=True)
def _coarse_shade() -> Iterator[None]:
    """Shrink the 055 shade raster to a handful of pixels — the shader's numeric correctness
    is pinned at full resolution in tests/unit/test_terrain_shade.py; here the subject is the
    terrain.json schema, so a coarse step keeps this module-scoped export cheap."""

    import obi.core.terrain as terrain_mod

    original = terrain_mod.SHADE_STEP_M
    terrain_mod.SHADE_STEP_M = 4000.0
    try:
        yield
    finally:
        terrain_mod.SHADE_STEP_M = original


class FakeGeometrySource:
    def geometry(self, *, timeout: float) -> object:
        return geometry_snapshot()


class FakeElevation:
    def ground(self, site_name: str, lat: float, lon: float) -> tuple[float, GroundProvenance]:
        return 1190.66, GroundProvenance.CACHE


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> tuple[ArGeometry, Path, Path]:
    tmp_path = tmp_path_factory.mktemp("terrain-contract")
    tile = flat_tile_bytes(1190.66)
    out_path = tmp_path / "links.json"
    geometry = ar_export(
        FakeGeometrySource(),  # type: ignore[arg-type]
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        controller="https://uisp.example.com",
        settings=WebSettings(gnis_states=["NV"]),
        elevation=FakeElevation(),  # type: ignore[arg-type]
        out_path=out_path,
        terrain=TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: tile),
        gnis=GnisSource(tmp_path, fetcher=lambda state: fixture_zip()),
    )
    return geometry, out_path, terrain_path_for(out_path)


def test_terrain_artifact_validates_against_the_normative_schema(
    exported: tuple[ArGeometry, Path, Path],
) -> None:
    _geometry, _links, terrain_path = exported
    payload = json.loads(terrain_path.read_text())
    jsonschema.validate(payload, json.loads(SCHEMA_PATH.read_text()))


def test_terrain_carries_a_shade_registration_block(
    exported: tuple[ArGeometry, Path, Path],
) -> None:
    """055: the flat-tile export produces a hillshade artifact, so terrain.json gains a
    ``shade`` block — per-site ENU rectangles + image dimensions (the widget's only input)."""

    geometry, _links, terrain_path = exported
    shade = json.loads(terrain_path.read_text())["shade"]
    assert shade["width"] >= 1 and shade["height"] >= 1
    assert shade["step_m"] > 0
    exported_sites = {site.name for site in geometry.sites}
    assert set(shade["sites"]) <= exported_sites
    assert shade["sites"], "flat tiles cover every site ⇒ a rect for each"
    for rect in shade["sites"].values():
        assert len(rect) == 4  # (e_min, n_min, e_max, n_max) in that site's ENU frame


def test_terrain_lives_beside_the_export_and_is_not_indented(
    exported: tuple[ArGeometry, Path, Path],
) -> None:
    _geometry, links_path, terrain_path = exported
    assert terrain_path.parent == links_path.parent
    assert terrain_path.name == "terrain.json"
    assert not terrain_path.with_name(terrain_path.name + ".tmp").exists()


def test_every_exported_link_has_a_profile_or_a_named_omission(
    exported: tuple[ArGeometry, Path, Path],
) -> None:
    geometry, links_path, terrain_path = exported
    profiles = json.loads(terrain_path.read_text())["profiles"]
    links = json.loads(links_path.read_text())["links"]
    omitted = {entry["item"] for entry in json.loads(links_path.read_text())["terrain_omitted"]}
    assert links
    for link in links:
        assert link["id"] in profiles or link["name"] in omitted  # SC-001: never silent


def test_profile_ends_equal_the_exported_site_grounds(
    exported: tuple[ArGeometry, Path, Path],
) -> None:
    """The datum-anchoring contract: sample 0 / n−1 match the resolving grounds exactly."""

    geometry, links_path, terrain_path = exported
    profiles = json.loads(terrain_path.read_text())["profiles"]
    payload = json.loads(links_path.read_text())
    site_ground = {site["name"]: site["ground_elevation_m"] for site in payload["sites"]}
    endpoint_site = {e["name"]: e["site"] for e in payload["endpoints"]}
    checked = 0
    for link in payload["links"]:
        profile = profiles.get(link["id"])
        if profile is None:
            continue
        samples = profile["samples_amsl"]
        assert samples[0] == round(site_ground[endpoint_site[link["a"]]], 1)
        assert samples[-1] == round(site_ground[endpoint_site[link["b"]]], 1)
        checked += 1
    assert checked > 0


def test_every_site_has_a_grid_with_matching_post_count(
    exported: tuple[ArGeometry, Path, Path],
) -> None:
    geometry, links_path, terrain_path = exported
    sites = json.loads(terrain_path.read_text())["sites"]
    exported_sites = {site["name"] for site in json.loads(links_path.read_text())["sites"]}
    assert set(sites) == exported_sites  # flat tiles cover everything ⇒ no omissions
    for site in sites.values():
        assert len(site["rel_heights_dm"]) == site["posts"] ** 2
        assert set(site["rel_heights_dm"]) == {0}  # flat DEM, self-referenced datum


def test_peaks_inside_a_site_extent_carry_names_and_enu(
    exported: tuple[ArGeometry, Path, Path],
) -> None:
    _geometry, _links, terrain_path = exported
    sites = json.loads(terrain_path.read_text())["sites"]
    peak_names = {peak["name"] for site in sites.values() for peak in site["peaks"]}
    # The GNIS fixture's summits are in the Black Rock area; whether any falls inside a
    # site extent depends on the recorded fleet, but the structure must always hold.
    for site in sites.values():
        for peak in site["peaks"]:
            assert peak["name"]
            assert len(peak["enu"]) == 3
    assert peak_names <= {"Old Razorback Mountain", "Elephant Mountain"}


def test_terrain_failure_leaves_prior_artifact_intact(tmp_path: Path) -> None:
    tile = flat_tile_bytes(1190.66)
    out_path = tmp_path / "links.json"
    common = dict(
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        controller="https://uisp.example.com",
        settings=WebSettings(),
        elevation=FakeElevation(),
        out_path=out_path,
    )
    ar_export(
        FakeGeometrySource(),  # type: ignore[arg-type]
        terrain=TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: tile),
        **common,  # type: ignore[arg-type]
    )
    terrain_path = terrain_path_for(out_path)
    before = terrain_path.read_bytes()

    # Next export: tiles gone (fetcher fails, fresh cache dir) ⇒ omissions, prior intact.
    geometry = ar_export(
        FakeGeometrySource(),  # type: ignore[arg-type]
        terrain=TerrainTileSource(tmp_path / "no-tiles", fetcher=lambda z, x, y: None),
        **common,  # type: ignore[arg-type]
    )
    assert geometry.terrain_omitted
    # links.json shipped, but the fully-empty rebuild must NOT clobber the last good
    # terrain artifact (the durable cache normally guarantees regeneration succeeds).
    assert terrain_path.read_bytes() == before
