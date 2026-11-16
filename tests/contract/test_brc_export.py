"""Contract tests for the BRC city-grid export (feature 098, contracts/brc-artifacts.md).

Offline by construction: the GeoJSON under tests/fixtures/brc/ is hand-trimmed shapes in
the published dataset's SHAPE (never the published data — its license stays out of the
repo, research D1), and the fetcher is injectable, so the suite needs no network. The
fixture README documents the hand-computed projection anchor the projection test uses.
"""

from __future__ import annotations

import json
import math
import shutil
from datetime import UTC, datetime
from pathlib import Path

from obi.core.brc import (
    SOURCE_FILES,
    BrcSource,
    build_brc_document,
)
from obi.core.geo import enu_offset
from obi.core.models import ArGeometry, ArSite, BrcFeatureKind, GroundProvenance
from obi.core.serialize import brc_to_dict, brc_to_json
from tests.fixtures.brc.origin import ORIGIN, SPIKE_LAT, SPIKE_LON, YEAR

FIXTURES = Path(__file__).parent.parent / "fixtures" / "brc"

# The street fixture's open LineString starts at spike + (0.002° lat, 0° lon) — see the
# fixture README's hand computation, independent of geo.enu_offset.
HAND_EAST_M = 0.0
HAND_NORTH_M = 222.39


def _dead_fetcher(name: str) -> bytes | None:
    raise AssertionError(f"fetcher must not be called (cache expected) for {name!r}")


def _warm_source(tmp_path: Path) -> BrcSource:
    """A BrcSource whose cache already holds every fixture file (fetcher must stay idle)."""

    cache = tmp_path / "brc-2026"
    cache.mkdir(parents=True, exist_ok=True)
    for fixture in FIXTURES.glob("*.geojson"):
        shutil.copy(fixture, cache / fixture.name)
    return BrcSource(cache, origin=ORIGIN, year=YEAR, fetcher=_dead_fetcher)


def _site(name: str, lat: float, lon: float, reach_m: float = 0.0) -> ArSite:
    enu = {"radio": (reach_m, 0.0, 10.0)} if reach_m else {}
    return ArSite(
        name=name,
        latitude=lat,
        longitude=lon,
        ground_elevation_m=1191.0,
        ground_provenance=GroundProvenance.OVERRIDE,
        enu=enu,
    )


def _geometry(*sites: ArSite) -> ArGeometry:
    return ArGeometry(
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        controller="https://uisp.test",
        sites=list(sites),
        endpoints=[],
        links=[],
    )


def _feature(document: object, kind: BrcFeatureKind):  # noqa: ANN201 - test helper
    matches = [f for f in document.features if f.kind is kind]
    assert len(matches) == 1, f"expected exactly one {kind} feature, got {len(matches)}"
    return matches[0]


# --- projection (spec FR-002/FR-003, research D4) ---------------------------------------------


def test_projection_matches_enu_offset_exactly(tmp_path: Path) -> None:
    """Every vertex is round(enu_offset(spike → vertex) × 10) — the pipeline's own geodesy."""

    document, _params = build_brc_document(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), _warm_source(tmp_path)
    )
    streets = _feature(document, BrcFeatureKind.STREETS)
    east, north = enu_offset(SPIKE_LAT, SPIKE_LON, 40.785242, -114.815579)
    assert streets.lines[0][0] == round(east * 10)
    assert streets.lines[0][1] == round(north * 10)


def test_projection_matches_hand_computed_values(tmp_path: Path) -> None:
    """Independent check against the fixture README's hand math (±0.3 m)."""

    document, _params = build_brc_document(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), _warm_source(tmp_path)
    )
    streets = _feature(document, BrcFeatureKind.STREETS)
    assert abs(streets.lines[0][0] / 10 - HAND_EAST_M) <= 0.3
    assert abs(streets.lines[0][1] / 10 - HAND_NORTH_M) <= 0.3


def test_polygon_rings_stay_closed(tmp_path: Path) -> None:
    document, _params = build_brc_document(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), _warm_source(tmp_path)
    )
    fence = _feature(document, BrcFeatureKind.TRASH_FENCE)
    ring = fence.lines[0]
    assert len(ring) == 12  # 6 coordinate pairs: 5 corners + the closing repeat
    assert ring[0] == ring[-2] and ring[1] == ring[-1]


def test_far_vertex_drops_its_whole_polyline_and_is_counted(tmp_path: Path) -> None:
    """The corrupt street fixture (a ~1,600 km vertex) never becomes geometry (15 km rule)."""

    document, params = build_brc_document(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), _warm_source(tmp_path)
    )
    streets = _feature(document, BrcFeatureKind.STREETS)
    # the open LineString + the block ring + two MultiLineString parts; corrupt one gone
    assert len(streets.lines) == 4
    assert params["dropped_lines"] == 1


def test_null_geometry_feature_is_skipped_not_fatal(tmp_path: Path) -> None:
    document, _params = build_brc_document(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), _warm_source(tmp_path)
    )
    plazas = _feature(document, BrcFeatureKind.PLAZAS)
    assert len(plazas.lines) == 1


def test_multilinestring_yields_one_polyline_per_part(tmp_path: Path) -> None:
    document, _params = build_brc_document(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), _warm_source(tmp_path)
    )
    streets = _feature(document, BrcFeatureKind.STREETS)
    parts = [line for line in streets.lines if len(line) == 4]  # the two 2-vertex MLS parts
    assert len(parts) == 2


def test_kind_with_no_surviving_lines_is_omitted(tmp_path: Path) -> None:
    """A source file whose every polyline drops contributes NO feature (data-model rule)."""

    source = _warm_source(tmp_path)
    corrupt = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[-100.0, 45.0], [0.0, 0.0]]},
            }
        ],
    }
    (source.cache_dir / SOURCE_FILES[BrcFeatureKind.PLAZAS]).write_text(
        json.dumps(corrupt), encoding="utf-8"
    )
    document, _params = build_brc_document(_geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), source)
    assert not any(f.kind is BrcFeatureKind.PLAZAS for f in document.features)
    assert any(f.kind is BrcFeatureKind.STREETS for f in document.features)


# --- per-site anchors (research D4/D6) --------------------------------------------------------


def test_anchor_is_the_exact_spike_offset_in_the_site_frame(tmp_path: Path) -> None:
    site = _site("mast", SPIKE_LAT + 0.014, SPIKE_LON + 0.027, reach_m=9_000.0)
    document, _params = build_brc_document(_geometry(site), _warm_source(tmp_path))
    east, north = enu_offset(site.latitude, site.longitude, SPIKE_LAT, SPIKE_LON)
    anchor = document.sites["mast"]
    assert anchor.east_m == round(east, 1)
    assert anchor.north_m == round(north, 1)
    assert math.hypot(anchor.east_m, anchor.north_m) > 1_000  # a real offset, not ~zero


# --- BrcSource cache discipline (research D1, data-model §BrcSource) --------------------------


def test_cache_hit_never_calls_the_fetcher(tmp_path: Path) -> None:
    source = _warm_source(tmp_path)  # _dead_fetcher raises if consulted
    for name in SOURCE_FILES.values():
        assert source.geojson(name) is not None


def test_cold_cache_fetches_once_then_reads_the_cache_forever(tmp_path: Path) -> None:
    cache = tmp_path / "brc-2026"
    calls: list[str] = []

    def fetcher(name: str) -> bytes | None:
        calls.append(name)
        return (FIXTURES / name).read_bytes()

    source = BrcSource(cache, origin=ORIGIN, year=YEAR, fetcher=fetcher)
    name = SOURCE_FILES[BrcFeatureKind.TRASH_FENCE]
    assert source.geojson(name) is not None
    assert calls == [name]
    assert (cache / name).is_file()  # durably cached (Principle VI)
    assert source.geojson(name) is not None
    assert calls == [name]  # same instance: no refetch
    fresh = BrcSource(cache, origin=ORIGIN, year=YEAR, fetcher=_dead_fetcher)
    assert fresh.geojson(name) is not None  # new instance: cache only, fetcher idle


def test_fetch_failure_returns_none_and_is_recorded(tmp_path: Path) -> None:
    source = BrcSource(tmp_path / "brc-2026", origin=ORIGIN, year=YEAR, fetcher=lambda name: None)
    name = SOURCE_FILES[BrcFeatureKind.TRASH_FENCE]
    assert source.geojson(name) is None
    assert source.outcomes[name] == "unavailable"


def test_unreadable_cache_file_is_refetched(tmp_path: Path) -> None:
    cache = tmp_path / "brc-2026"
    cache.mkdir(parents=True)
    name = SOURCE_FILES[BrcFeatureKind.TRASH_FENCE]
    (cache / name).write_text("not json {", encoding="utf-8")
    source = BrcSource(
        cache, origin=ORIGIN, year=YEAR, fetcher=lambda n: (FIXTURES / n).read_bytes()
    )
    assert source.geojson(name) is not None
    assert source.outcomes[name] == "fetched"
    assert json.loads((cache / name).read_text(encoding="utf-8"))["type"] == "FeatureCollection"


# --- wire shape (data-model §wire shape) ------------------------------------------------------


def test_wire_shape_matches_the_data_model(tmp_path: Path) -> None:
    document, _params = build_brc_document(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), _warm_source(tmp_path)
    )
    payload = brc_to_dict(document)
    assert payload["year"] == 2026
    assert payload["spike"] == [SPIKE_LAT, SPIKE_LON]
    assert isinstance(payload["generated_at"], str)
    kinds = {feature["kind"] for feature in payload["features"]}
    assert kinds == {"streets", "trash_fence", "plazas"}
    for feature in payload["features"]:
        assert all(isinstance(v, int) for line in feature["lines"] for v in line)
    assert payload["sites"] == {"playa": {"east_m": 0.0, "north_m": 0.0}}


def test_json_is_compact_by_default(tmp_path: Path) -> None:
    document, _params = build_brc_document(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), _warm_source(tmp_path)
    )
    text = brc_to_json(document)
    assert "\n" not in text  # coordinates dominate; whitespace would double the artifact
    assert json.loads(text)["year"] == 2026


# --- engine step (contract C1) ----------------------------------------------------------------


def test_brc_path_is_beside_the_export() -> None:
    from obi.core.engine import brc_path_for

    assert brc_path_for(Path("/state/ar/links.json")) == Path("/state/ar/brc.json")


def test_step_writes_the_artifact_atomically(tmp_path: Path) -> None:
    from obi.core.engine import _apply_brc_step

    out = tmp_path / "brc.json"
    params = _apply_brc_step(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)),
        brc=_warm_source(tmp_path),
        out_path=out,
    )
    assert params["status"] == "written"
    assert params["path"] == str(out)
    assert params["sites_in_range"] == ["playa"]
    assert params["features"]["trash_fence"] == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["year"] == 2026
    assert not list(tmp_path.glob("*.tmp"))  # temp+replace, nothing left behind


def test_step_disabled_writes_nothing(tmp_path: Path) -> None:
    from obi.core.engine import _apply_brc_step

    out = tmp_path / "brc.json"
    params = _apply_brc_step(_geometry(), brc=None, out_path=out)
    assert params == {"status": "disabled"}
    assert not out.exists()


def test_step_keeps_the_last_good_artifact_when_nothing_is_buildable(tmp_path: Path) -> None:
    """Cache gone + offline ⇒ the prior brc.json survives (the 031 keep-last-good rule)."""

    from obi.core.engine import _apply_brc_step

    out = tmp_path / "brc.json"
    out.write_text('{"sentinel": true}', encoding="utf-8")
    dead = BrcSource(tmp_path / "empty-cache", origin=ORIGIN, year=YEAR, fetcher=lambda name: None)
    params = _apply_brc_step(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), brc=dead, out_path=out
    )
    assert params["status"] == "kept_prior"
    assert "unavailable" in str(params["sources"])
    assert json.loads(out.read_text(encoding="utf-8")) == {"sentinel": True}


def test_step_never_raises_even_on_a_broken_source(tmp_path: Path) -> None:
    from obi.core.engine import _apply_brc_step

    class Boom:
        def geojson(self, name: str) -> None:
            raise RuntimeError("catastrophic")

    params = _apply_brc_step(_geometry(), brc=Boom(), out_path=tmp_path / "brc.json")
    assert params["status"] == "error"
    assert "catastrophic" in str(params["error"])


def test_ar_export_carries_the_brc_seam(tmp_path: Path) -> None:
    """The export signature accepts brc (default None) — the web_export wiring's contract."""

    import inspect

    from obi.core.engine import ar_export

    parameter = inspect.signature(ar_export).parameters.get("brc")
    assert parameter is not None
    assert parameter.default is None


# --- US3: gating + graceful absence (spec US3, FR-007, research D6) ---------------------------


def test_out_of_range_site_gets_no_anchor_while_in_range_does(tmp_path: Path) -> None:
    """The gate is the site's own grid half-extent — ONE definition of viewing range (D6)."""

    near = _site(
        "near", SPIKE_LAT + 0.014, SPIKE_LON + 0.027, reach_m=9_000.0
    )  # ~3 km out, 11 km half-extent
    far = _site("far", SPIKE_LAT - 0.486, SPIKE_LON - 0.004)  # ~54 km out, minimum 2 km half-extent
    document, params = build_brc_document(_geometry(near, far), _warm_source(tmp_path))
    assert set(document.sites) == {"near"}
    assert params["sites_in_range"] == ["near"]


def test_all_sites_out_of_range_still_writes_a_valid_empty_sites_doc(tmp_path: Path) -> None:
    """sites: {} is a VALID artifact — "city exists, nobody's in range" (data-model)."""

    from obi.core.engine import _apply_brc_step

    out = tmp_path / "brc.json"
    params = _apply_brc_step(
        _geometry(_site("far", SPIKE_LAT - 0.486, SPIKE_LON - 0.004)),
        brc=_warm_source(tmp_path),
        out_path=out,
    )
    assert params["status"] == "written"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["sites"] == {}
    assert payload["features"]  # the city itself still ships


def test_one_corrupt_source_file_degrades_to_the_readable_kinds(tmp_path: Path) -> None:
    """Partial degrade: a bad plazas file costs the plazas, never the city (spec US3 #3)."""

    from obi.core.engine import _apply_brc_step

    source = _warm_source(tmp_path)
    (source.cache_dir / SOURCE_FILES[BrcFeatureKind.PLAZAS]).write_text(
        "not json {", encoding="utf-8"
    )
    broken = BrcSource(source.cache_dir, origin=ORIGIN, year=YEAR, fetcher=lambda name: None)
    out = tmp_path / "brc.json"
    params = _apply_brc_step(
        _geometry(_site("playa", SPIKE_LAT, SPIKE_LON)), brc=broken, out_path=out
    )
    assert params["status"] == "written"
    sources = params["sources"]
    assert sources[SOURCE_FILES[BrcFeatureKind.PLAZAS]] == "unavailable"
    payload = json.loads(out.read_text(encoding="utf-8"))
    kinds = {feature["kind"] for feature in payload["features"]}
    assert kinds == {"streets", "trash_fence"}
