"""Black Rock City survey geometry for the XR viewer (feature 098) — fetched once, cached durably.

Burning Man's Innovate program publishes the post-golden-spike survey of Black Rock City as
GeoJSON (the event's published GIS data repository). obi downloads the files the
city-grid layer needs **once**, caches them under the AR state dir, and builds a compact
scene document from the cache — every later export works offline (Principle VI, the
GNIS/elevation cache shape). The dataset's LICENSE points at Burning Man's Terms of Service
with no redistribution grant, so the files are cached locally, never vendored, and the test
fixtures are hand-trimmed shapes (research D1).

The survey origin, plan year and source URL are the operator's ``[brc]`` config (feature 137 —
no built-in event location). The city is projected ONCE into an ENU frame centered on that
origin (the golden spike); each in-range site gets the spike's exact offset in its own frame
(research D4 — the cross-frame flat-earth residual is ≤ a few meters, inside the spec's 10 m
tolerance). The fetcher is injectable so tests run offline against the fixtures.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from obi.core.geo import enu_offset
from obi.core.models import (
    ArGeometry,
    BrcDocument,
    BrcFeature,
    BrcFeatureKind,
    BrcSiteAnchor,
)
from obi.core.terrain import grid_half_extent

logger = logging.getLogger("obi")

# Feature kind → published source file (2026/GeoJSON). Street OUTLINES, not centerlines
# (research D3); the centerline file (street_lines.geojson) is the documented fallback if
# the field pass finds outlines noisy at far zoom — a one-name swap here.
SOURCE_FILES: dict[BrcFeatureKind, str] = {
    BrcFeatureKind.STREETS: "street_outlines.geojson",
    BrcFeatureKind.TRASH_FENCE: "trash_fence.geojson",
    BrcFeatureKind.PLAZAS: "plazas.geojson",
    # gate_road.geojson was consumed through field round 1; dropped by operator decision in
    # field round 2 (a stale cached copy is harmless — nothing requests it).
}

# A vertex farther than this from the spike is corrupt/foreign data, never city geometry;
# its whole polyline is dropped and counted (data-model §BrcFeature) — the fence is ~4 km out.
_SANITY_RADIUS_M = 15_000.0

# A fetcher returns the raw GeoJSON bytes for a published file name, or None on failure.
BrcFetcher = Callable[[str], bytes | None]


def _source_url(template: str, year: int, name: str) -> str:
    """``[brc] source_url`` → one file's URL: ``{year}``/``{name}`` are substituted; a template
    without ``{name}`` is a directory the file name is appended to."""

    url = template.replace("{year}", str(year))
    return url.replace("{name}", name) if "{name}" in url else f"{url.rstrip('/')}/{name}"


def _default_fetcher(source_url: str | None, year: int, timeout: float) -> BrcFetcher:
    if source_url is None:
        return lambda name: None  # no source configured ⇒ the local cache only

    def fetch(name: str) -> bytes | None:
        url = _source_url(source_url, year, name)
        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            logger.debug("BRC fetch failed for %s (%s): %s", name, url, exc)
            return None

    return fetch


class BrcSource:
    """Durable-cached access to the published city GeoJSON (the GnisSource shape).

    A file present in the cache is NEVER re-fetched — the manual-sideload contract
    (quickstart §1) and the offline guarantee in one rule. ``outcomes`` records, per file
    name, how this instance satisfied (or failed) each request: ``"cached"`` /
    ``"fetched"`` / ``"unavailable"`` — surfaced in the export's audit params.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        origin: tuple[float, float],
        year: int,
        source_url: str | None = None,
        fetcher: BrcFetcher | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.cache_dir = cache_dir
        self.origin = origin  # the survey origin (lat, lon): the frame every point projects into
        self.year = year
        self.outcomes: dict[str, str] = {}
        self._fetch = fetcher or _default_fetcher(source_url, year, timeout)

    @classmethod
    def from_settings(
        cls, settings: Any, web_dir: Path, *, fetcher: BrcFetcher | None = None
    ) -> BrcSource:
        """The source for a ``[brc]`` :class:`~obi.core.config.BrcSettings`."""

        return cls(
            web_dir / settings.cache_name,
            origin=(settings.origin_lat, settings.origin_lon),
            year=settings.year,
            source_url=settings.source_url,
            fetcher=fetcher,
        )

    def geojson(self, name: str) -> dict[str, object] | None:
        """The named file's parsed FeatureCollection, fetching + caching once; None = unavailable.

        An unreadable cache file is treated as missing (refetched); a fetch failure with no
        cache is the degraded mode — the caller records the omission. Never raises.
        """

        path = self.cache_dir / name
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.outcomes[name] = "cached"
                    return raw
            except (OSError, ValueError):
                logger.warning("BRC cache unreadable, refetching: %s", path)
        data = self._fetch(name)
        if data is None:
            self.outcomes[name] = "unavailable"
            return None
        try:
            raw = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning("BRC file %s is not JSON; treating as unavailable", name)
            self.outcomes[name] = "unavailable"
            return None
        if not isinstance(raw, dict):
            self.outcomes[name] = "unavailable"
            return None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:  # cache write failure degrades, never fails the export
            logger.warning("could not write BRC cache %s: %s", path, exc)
        self.outcomes[name] = "fetched"
        return raw


def _coordinate_arrays(geometry: object) -> list[list[object]]:
    """Every polyline-shaped coordinate array in one GeoJSON geometry.

    LineString → itself; MultiLineString → parts; Polygon → rings (closed, kept as-is);
    MultiPolygon → every ring of every polygon. Anything else contributes nothing.
    """

    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return []
    if kind == "LineString":
        return [coordinates]
    if kind in ("MultiLineString", "Polygon"):
        return [part for part in coordinates if isinstance(part, list)]
    if kind == "MultiPolygon":
        return [
            ring
            for polygon in coordinates
            if isinstance(polygon, list)
            for ring in polygon
            if isinstance(ring, list)
        ]
    return []


def _project_polyline(coordinates: list[object], origin: tuple[float, float]) -> list[int] | None:
    """One [lon, lat] array → a flat spike-frame [e, n, …] decimeter polyline, or None.

    A single unparsable, non-finite, or beyond-sanity vertex invalidates the WHOLE
    polyline (corrupt data is dropped and counted, never repaired — data-model rule).
    """

    line: list[int] = []
    for pair in coordinates:
        if not isinstance(pair, list) or len(pair) < 2:
            return None
        try:
            lon, lat = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(lon) and math.isfinite(lat)):
            return None
        east, north = enu_offset(origin[0], origin[1], lat, lon)
        if math.hypot(east, north) > _SANITY_RADIUS_M:
            return None
        line.extend((round(east * 10), round(north * 10)))
    return line if len(line) >= 4 else None  # a polyline needs at least two vertices


def build_brc_document(
    geometry: ArGeometry, source: BrcSource, *, now: datetime | None = None
) -> tuple[BrcDocument, dict[str, object]]:
    """The city document + its build params (feature counts, drops, sites in range).

    Features come from the durable cache (fetched once when cold); sites earn an anchor iff
    the spike lies within their grid half-extent — the one existing definition of "how far
    this site can see" (research D6). Missing/corrupt source files degrade to whatever
    remains readable; the caller decides what an empty document means (contract C1).
    """

    features: list[BrcFeature] = []
    dropped = 0
    for kind, name in SOURCE_FILES.items():
        collection = source.geojson(name)
        if collection is None:
            continue
        raw_features = collection.get("features")
        lines: list[list[int]] = []
        for feature in raw_features if isinstance(raw_features, list) else []:
            if not isinstance(feature, dict):
                continue
            for coordinates in _coordinate_arrays(feature.get("geometry")):
                projected = _project_polyline(coordinates, source.origin)
                if projected is None:
                    dropped += 1
                else:
                    lines.append(projected)
        if lines:
            features.append(BrcFeature(kind=kind, lines=lines))

    sites: dict[str, BrcSiteAnchor] = {}
    for site in geometry.sites:
        east, north = enu_offset(site.latitude, site.longitude, *source.origin)
        if math.hypot(east, north) <= grid_half_extent(site.enu):
            sites[site.name] = BrcSiteAnchor(east_m=round(east, 1), north_m=round(north, 1))

    document = BrcDocument(
        generated_at=now or datetime.now(UTC),
        year=source.year,
        spike=source.origin,
        features=features,
        sites=sites,
    )
    params: dict[str, object] = {
        "sources": dict(source.outcomes),
        "features": {feature.kind.value: len(feature.lines) for feature in features},
        "dropped_lines": dropped,
        "sites_in_range": sorted(sites),
    }
    return document, params
