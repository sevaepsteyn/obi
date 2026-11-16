"""Web-export geometry source (feature 134, T037): relocate, re-ground, regenerate terrain.

The live ``links.json`` is rebuilt as an :class:`~obi.core.models.ArGeometry`, every site and
endpoint moved by the rigid relocation, renamed, and re-grounded on the DESTINATION terrain:
each endpoint keeps its height above its site's ground (mast heights preserved, FR-012) on top
of the new ground elevation. Terrain profiles, Fresnel verdicts, grids, hillshade and peaks are
then RECOMPUTED there by the engine's terrain step against a demo-only cache
(``[demo] terrain_cache_dir`` — never obi's live web dir, analyze C3); nothing terrain-shaped is
copied from the live export. The city-grid artifact (``brc.json``) is never produced.

A destination that cannot be grounded or profiled (DEM/EPQS unreachable, nothing cached) fails the
run with :class:`~obi.core.demo.SourceError` — a demo with fallback terrain would misrepresent
the Fresnel story.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from obi.core.demo import SourceError
from obi.core.demo.sources import ABSENT, INCLUDED, SourceContext, SourceReport
from obi.core.elevation import ElevationSource
from obi.core.engine import regenerate_terrain
from obi.core.geo import enu_offset
from obi.core.gnis import GnisSource
from obi.core.models import (
    ArEndpoint,
    ArGeometry,
    ArLink,
    ArSite,
    FrequencyProvenance,
    GroundProvenance,
    HeightProvenance,
    LinkHealth,
    LocationProvenance,
)
from obi.core.serialize import ar_geometry_to_dict
from obi.core.terrain import TerrainTileSource

NAME = "geo"
_DEFAULT_GROUND = 1200.0


def _sites(data: dict[str, Any]) -> list[ArSite]:
    return [
        ArSite(
            name=str(s["name"]),
            latitude=float(s["latitude"]),
            longitude=float(s["longitude"]),
            ground_elevation_m=float(s["ground_elevation_m"]),
            ground_provenance=GroundProvenance(s["ground_provenance"]),
        )
        for s in data.get("sites", [])
    ]


def _endpoints(data: dict[str, Any]) -> list[ArEndpoint]:
    return [
        ArEndpoint(
            name=str(e["name"]),
            site=str(e["site"]),
            latitude=float(e["latitude"]),
            longitude=float(e["longitude"]),
            raw_altitude=e.get("raw_altitude"),
            height_amsl_m=float(e["height_amsl_m"]),
            height_provenance=HeightProvenance(e["height_provenance"]),
            status=str(e.get("status", "unknown")),
            link_score=e.get("link_score"),
            maintenance=bool(e.get("maintenance", False)),
            location_provenance=LocationProvenance(e.get("location_provenance", "device")),
        )
        for e in data.get("endpoints", [])
    ]


def _links(data: dict[str, Any]) -> list[ArLink]:
    links = []
    for raw in data.get("links", []):
        provenance = raw.get("frequency_provenance")
        links.append(
            ArLink(
                id=str(raw["id"]),
                name=str(raw["name"]),
                a=str(raw["a"]),
                b=str(raw["b"]),
                band=str(raw["band"]),
                health=LinkHealth(raw["health"]),
                signal_dbm=raw.get("signal_dbm"),
                length_m=float(raw["length_m"]),
                frequency_ghz=raw.get("frequency_ghz"),
                frequency_provenance=FrequencyProvenance(provenance) if provenance else None,
                fresnel_radius_m=raw.get("fresnel_radius_m"),
                capacity_down_bps=raw.get("capacity_down_bps"),
                capacity_up_bps=raw.get("capacity_up_bps"),
                usage_down_bps=raw.get("usage_down_bps"),
                usage_up_bps=raw.get("usage_up_bps"),
                maintenance=bool(raw.get("maintenance", False)),
                source=str(raw.get("source", "uisp")),
            )
        )
    return links


def run(ctx: SourceContext) -> SourceReport:
    src = ctx.state_dir / "ar" / "links.json"
    if not src.exists():
        return SourceReport(NAME, ABSENT)
    data: dict[str, Any] = json.loads(src.read_text(encoding="utf-8"))
    rw, reloc = ctx.rewriter, ctx.relocator
    cache = ctx.profile.terrain_cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    web = ctx.raw_config.get("web")
    default_ground = (
        float(web.get("default_ground_elevation", _DEFAULT_GROUND))
        if isinstance(web, dict)
        else _DEFAULT_GROUND
    )
    elevation_fetcher = cast(Callable[[float, float], float | None] | None,
                             ctx.fetchers.get("elevation"))  # fmt: skip
    elevation = ElevationSource(
        cache / "elevations.json",
        default_ground_elevation=default_ground,
        fetcher=elevation_fetcher,
    )

    old_ground: dict[str, float] = {}
    new_ground: dict[str, float] = {}
    sites = _sites(data)
    for site in sites:
        old_ground[site.name] = site.ground_elevation_m
        site.name = rw.ident(site.name)
        site.latitude, site.longitude = reloc.point(site.latitude, site.longitude)
        ground, provenance = elevation.ground(site.name, site.latitude, site.longitude)
        if provenance is GroundProvenance.DEFAULT_GROUND:
            raise SourceError("could not ground the relocated sites (USGS EPQS unreachable)")
        # Reported uniformly as CACHE: every value is served from the demo terrain cache, and a
        # first-run "epqs" vs a re-run "cache" would make otherwise identical datasets differ.
        site.ground_elevation_m, site.ground_provenance = round(ground, 2), GroundProvenance.CACHE
        new_ground[site.name] = site.ground_elevation_m

    endpoints = _endpoints(data)
    for ep in endpoints:
        real_site = ep.site
        agl = ep.height_amsl_m - old_ground.get(real_site, ep.height_amsl_m)
        ep.name, ep.site = rw.ident(ep.name), rw.ident(real_site)
        ep.latitude, ep.longitude = reloc.point(ep.latitude, ep.longitude)
        base = new_ground.get(ep.site)
        if base is None:
            base, _prov = elevation.ground(ep.site, ep.latitude, ep.longitude)
        ep.height_amsl_m = round(base + agl, 2)
        if ep.height_provenance is HeightProvenance.AMSL and ep.raw_altitude is not None:
            ep.raw_altitude = ep.height_amsl_m

    links = _links(data)
    for link in links:
        link.name, link.a, link.b = rw.ident(link.name), rw.ident(link.a), rw.ident(link.b)
        link.id = rw.ident(link.id)

    for site in sites:
        site.enu = {}
        for ep in sorted(endpoints, key=lambda e: e.name):
            east, north = enu_offset(site.latitude, site.longitude, ep.latitude, ep.longitude)
            up = ep.height_amsl_m - site.ground_elevation_m
            site.enu[ep.name] = (round(east, 2), round(north, 2), round(up, 2))

    geometry = ArGeometry(
        generated_at=ctx.shift.dt(datetime.fromisoformat(str(data["generated_at"]))),
        controller=rw.text(str(data.get("controller", ""))),
        sites=sites,
        endpoints=sorted(endpoints, key=lambda e: e.name),
        links=sorted(links, key=lambda link: link.name),
    )

    out_ar = ctx.out_state / "ar"
    out_ar.mkdir(parents=True, exist_ok=True)
    tile_fetcher = ctx.fetchers.get("tile")
    gnis_fetcher = ctx.fetchers.get("gnis")
    params = regenerate_terrain(
        geometry,
        web_dir=cache,
        gnis_states=[ctx.profile.gnis_state],
        terrain_out_path=out_ar / "terrain.json",
        terrain=TerrainTileSource(cache / "terrain-tiles", fetcher=tile_fetcher)  # type: ignore[arg-type]
        if tile_fetcher is not None
        else None,
        gnis=GnisSource(cache, fetcher=gnis_fetcher)  # type: ignore[arg-type]
        if gnis_fetcher is not None
        else None,
    )
    if "terrain_error" in params or (geometry.links and not params.get("profiles")):
        raise SourceError("could not regenerate terrain at the destination (DEM tiles unavailable)")

    # The terrain step stamps the wall clock (the run date — non-deterministic, and a date the
    # dataset should not carry): restamp both artifacts with the geometry's shifted time.
    stamp = geometry.generated_at.isoformat()
    terrain_path = out_ar / "terrain.json"
    if terrain_path.exists():
        # Summit names come from the public gazetteer AT THE DESTINATION — fictional-site
        # geography, not the real network's. They may share a word with a real site ("Black
        # Mountain"), so they are declared to the gate as public values.
        ctx.public_values.update(_peak_names(json.loads(terrain_path.read_text(encoding="utf-8"))))
    for name in ("terrain.json", "terrain-shade.meta.json"):
        path = out_ar / name
        if path.exists():
            path.write_text(
                json.dumps(_restamp(json.loads(path.read_text(encoding="utf-8")), stamp),
                           ensure_ascii=False),
                encoding="utf-8",
            )  # fmt: skip

    payload = ar_geometry_to_dict(geometry)
    for key in ("excluded", "degraded", "site_anchored", "tower_placed", "tower_unmatched"):
        payload[key] = rw.json(data.get(key, []), key)
    for placement in payload["tower_placed"]:
        site_ground = new_ground.get(str(placement.get("site")))
        if site_ground is not None and isinstance(placement.get("tower_height_m"), int | float):
            placement["height_amsl_m"] = round(site_ground + placement["tower_height_m"], 2)
            placement["raw_altitude"] = None
    (out_ar / "links.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    # The elevation cache ships (destination points only). The state-wide summit list does NOT:
    # it covers every summit in the state — including ones near the REAL network — and its
    # peaks are already resolved per site into terrain.json.
    if (cache / "elevations.json").exists():
        shutil.copyfile(cache / "elevations.json", out_ar / "elevations.json")
    verdicts = params.get("verdicts", {})
    clear = verdicts.get("clear", 0) if isinstance(verdicts, dict) else 0
    return SourceReport(
        NAME,
        INCLUDED,
        rows_in=len(data.get("links", [])),
        rows_out=len(links),
        notes={"fresnel_clear": int(clear), "profiles": int(str(params.get("profiles", 0)))},
    )


def _restamp(obj: Any, stamp: str) -> Any:
    if isinstance(obj, dict):
        return {k: stamp if k == "generated_at" else _restamp(v, stamp) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_restamp(v, stamp) for v in obj]
    return obj


def _peak_names(obj: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "peaks" and isinstance(value, list):
                names.update(str(p["name"]) for p in value if isinstance(p, dict) and p.get("name"))
            else:
                names.update(_peak_names(value))
    elif isinstance(obj, list):
        for item in obj:
            names.update(_peak_names(item))
    return names
