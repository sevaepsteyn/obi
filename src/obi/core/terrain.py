"""Terrain for the AR geometry (feature 031) — DEM tiles, link profiles, clearance, grids.

Ground elevations come from the public AWS Terrain Tiles dataset (terrarium-encoded PNGs,
keyless; US data derives from USGS 3DEP/NED — research R1), fetched **once per tile** and
cached durably as raw bytes (terrain is static), so every later export works with zero
internet (Principle VI, the 022 elevation-cache shape). The PNG decoder is deliberately
stdlib-only and narrow: non-interlaced 8-bit truecolor RGB/RGBA — exactly what terrarium
tiles are; anything else is no-data, never a guess.

Everything numeric the viewer displays is decided HERE (Principle III): profile sampling
and datum anchoring (research R2), the 4/3-effective-earth clearance verdicts (research
R3), and the per-site heightfield grids (research R4). The tile fetcher is injectable so
tests run fully offline against synthetic tiles.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from obi.core.geo import enu_offset, fresnel_local_radius_m
from obi.core.models import (
    ArGeometry,
    Clearance,
    ClearanceBasis,
    ClearanceVerdict,
    LinkProfile,
    Peak,
    SiteTerrain,
    TerrainDocument,
    TerrainOmission,
    TerrainShadeMeta,
    _utcnow,
)

logger = logging.getLogger("obi")

_TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

# One zoom for everything: ~29 m/px at the network's latitude — matches the ~30 m profile
# step, and a 24 km site extent needs ≤ 16 tiles (research R1).
ZOOM = 12
_TILE_SIZE = 256

# Same plausibility band as core/elevation.py: outside it is a no-data sentinel, not terrain.
_PLAUSIBLE_MIN_M = -430.0
_PLAUSIBLE_MAX_M = 9000.0

# Profile sampling (research R3): ~DEM-native step so no feature wider than a step is missed.
PROFILE_STEP_M = 30.0

# Site grid (research R4): fixed post count, extent from the site's endpoints, clamped.
GRID_POSTS = 161
GRID_MARGIN_M = 2000.0
GRID_HALF_EXTENT_MIN_M = 2000.0
GRID_HALF_EXTENT_MAX_M = 28000.0  # covers the 26 km event↔remote-site cluster (field, 2026-07-06)

# Clearance physics (research R3). k = 4/3 is the standard radio-refraction effective
# earth; 0.6 of the first Fresnel radius is the actionable clearance core (025).
_EFFECTIVE_EARTH_K = 4.0 / 3.0
_EARTH_RADIUS_M = 6_371_008.8  # matches core/geo.py
_CORE_FACTOR = 0.6

# A tile fetcher returns the raw PNG bytes for (zoom, x, y), or None on any failure.
TileFetcher = Callable[[int, int, int], bytes | None]


# --- terrarium PNG decoding (stdlib-only, research R1) -----------------------------------------


def _defilter_scanlines(raw: bytes, width: int, height: int, channels: int) -> bytearray | None:
    """Undo PNG scanline filters (types 0–4) over the inflated stream, or None if corrupt."""

    stride = width * channels
    if len(raw) != (stride + 1) * height:
        return None
    out = bytearray(stride * height)
    prev = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        filter_type = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        if filter_type == 1:  # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up = prev[i]
                up_left = prev[i - channels] if i >= channels else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                if pa <= pb and pa <= pc:
                    predictor = left
                elif pb <= pc:
                    predictor = up
                else:
                    predictor = up_left
                line[i] = (line[i] + predictor) & 0xFF
        elif filter_type != 0:  # unknown filter ⇒ corrupt
            return None
        out[row * stride : (row + 1) * stride] = line
        prev = line
    return out


def decode_terrarium(data: bytes) -> list[list[float]] | None:
    """Decode a terrarium tile to a ``[row][col]`` grid of meters, or None (no-data).

    Accepts exactly the terrarium shape: non-interlaced 8-bit truecolor (RGB or RGBA) PNG.
    Interlaced/paletted/16-bit/truncated/corrupt inputs are no-data — never a guess.
    Elevation per pixel: ``R·256 + G + B/256 − 32768``.
    """

    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    width = height = 0
    channels = 0
    idat: list[bytes] = []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        chunk_type = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        if len(chunk) < length:
            return None
        if chunk_type == b"IHDR":
            if length != 13:
                return None
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if bit_depth != 8 or interlace != 0 or color_type not in (2, 6):
                return None
            channels = 3 if color_type == 2 else 4
        elif chunk_type == b"IDAT":
            idat.append(chunk)
        elif chunk_type == b"IEND":
            break
        pos += 12 + length
    if not width or not height or not channels or not idat:
        return None
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return None
    pixels = _defilter_scanlines(raw, width, height, channels)
    if pixels is None:
        return None
    stride = width * channels
    grid: list[list[float]] = []
    for row in range(height):
        base = row * stride
        grid.append(
            [
                pixels[base + col * channels] * 256.0
                + pixels[base + col * channels + 1]
                + pixels[base + col * channels + 2] / 256.0
                - 32768.0
                for col in range(width)
            ]
        )
    return grid


# --- slippy-map addressing (pure math) ---------------------------------------------------------


def global_pixel(lat: float, lon: float, zoom: int = ZOOM) -> tuple[float, float]:
    """(x, y) in the zoom level's global pixel space (Web Mercator, 256 px tiles)."""

    n = float(1 << zoom) * _TILE_SIZE
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def tile_for(lat: float, lon: float, zoom: int = ZOOM) -> tuple[int, int]:
    """The (x, y) tile containing a coordinate at ``zoom``."""

    px, py = global_pixel(lat, lon, zoom)
    return int(px // _TILE_SIZE), int(py // _TILE_SIZE)


# --- tile source: durable cache + injectable fetcher (the elevation.py shape) -------------------


def _default_tile_fetcher(timeout: float) -> TileFetcher:
    def fetch(zoom: int, x: int, y: int) -> bytes | None:
        try:
            response = httpx.get(_TILE_URL.format(z=zoom, x=x, y=y), timeout=timeout)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            logger.debug("terrain tile fetch failed for %d/%d/%d: %s", zoom, x, y, exc)
            return None

    return fetch


class TerrainTileSource:
    """Terrarium tiles with a durable raw-PNG cache and per-run fetch/hit counters.

    Cache layout: ``<cache_dir>/<zoom>/<x>/<y>.png`` — the bytes exactly as fetched, never
    expiring (delete files to force a refetch). A cached tile that fails to decode is
    deleted (self-healing) and refetched; a fetched tile is cached only if it decodes.
    """

    def __init__(
        self, cache_dir: Path, *, fetcher: TileFetcher | None = None, timeout: float = 20.0
    ) -> None:
        self._cache_dir = cache_dir
        self._fetch = fetcher or _default_tile_fetcher(timeout)
        self._tiles: dict[tuple[int, int], list[list[float]] | None] = {}
        self.tiles_fetched = 0
        self.tiles_cached = 0

    def _tile_path(self, x: int, y: int) -> Path:
        return self._cache_dir / str(ZOOM) / str(x) / f"{y}.png"

    def tile(self, x: int, y: int) -> list[list[float]] | None:
        """The decoded elevation grid for tile (x, y), or None when unavailable."""

        key = (x, y)
        if key in self._tiles:
            return self._tiles[key]
        grid: list[list[float]] | None = None
        path = self._tile_path(x, y)
        if path.is_file():
            try:
                grid = decode_terrarium(path.read_bytes())
            except OSError:
                grid = None
            if grid is None:  # corrupt cache entry: self-heal, then try the network
                logger.warning("terrain tile cache corrupt, refetching: %s", path)
                with contextlib.suppress(OSError):
                    path.unlink()
            else:
                self.tiles_cached += 1
        if grid is None:
            data = self._fetch(ZOOM, x, y)
            if data is not None:
                grid = decode_terrarium(data)
                if grid is None:
                    logger.debug("terrain tile %d/%d undecodable; treated as no-data", x, y)
                else:
                    self.tiles_fetched += 1
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                    except OSError as exc:  # cache write failure degrades, never fails
                        logger.warning("could not write terrain tile cache %s: %s", path, exc)
        self._tiles[key] = grid
        return grid

    def elevation(self, lat: float, lon: float) -> float | None:
        """Bilinear ground elevation (m AMSL) at a coordinate, or None (no data).

        Interpolates between the four nearest pixel centers, crossing tile boundaries as
        needed; any missing tile, missing pixel, or implausible value ⇒ None.
        """

        px, py = global_pixel(lat, lon)
        # Pixel centers sit at +0.5; find the four surrounding centers.
        fx = px - 0.5
        fy = py - 0.5
        x0, y0 = math.floor(fx), math.floor(fy)
        wx = fx - x0
        wy = fy - y0
        total = 0.0
        weight_sum = 0.0
        for dy, wy_part in ((0, 1.0 - wy), (1, wy)):
            for dx, wx_part in ((0, 1.0 - wx), (1, wx)):
                weight = wx_part * wy_part
                if weight <= 0.0:
                    continue
                value = self._pixel(x0 + dx, y0 + dy)
                if value is None:
                    return None
                total += value * weight
                weight_sum += weight
        if weight_sum <= 0.0:
            return None
        elevation = total / weight_sum
        if not (_PLAUSIBLE_MIN_M <= elevation <= _PLAUSIBLE_MAX_M):
            return None
        return elevation

    def _pixel(self, gx: int, gy: int) -> float | None:
        limit = (1 << ZOOM) * _TILE_SIZE
        if not (0 <= gx < limit and 0 <= gy < limit):
            return None
        grid = self.tile(gx // _TILE_SIZE, gy // _TILE_SIZE)
        if grid is None:
            return None
        row = grid[gy % _TILE_SIZE]
        if gx % _TILE_SIZE >= len(row):
            return None
        return row[gx % _TILE_SIZE]


# --- link profiles + clearance (research R2/R3) -------------------------------------------------


def build_profile(
    source: TerrainTileSource,
    *,
    lat_a: float,
    lon_a: float,
    ground_a: float,
    lat_b: float,
    lon_b: float,
    ground_b: float,
    length_m: float,
) -> LinkProfile | None:
    """Datum-anchored ground profile along the chord, or None if ANY sample is missing.

    Anchoring (research R2): the correction at each end is the difference between the
    endpoint's already-exported resolving ground elevation and the DEM there, blended
    linearly along the path — a constant datum offset cancels exactly and the profile ends
    equal the exported grounds, so a radio never starts "underground".
    """

    dem_a = source.elevation(lat_a, lon_a)
    dem_b = source.elevation(lat_b, lon_b)
    if dem_a is None or dem_b is None:
        return None
    correction_a = ground_a - dem_a
    correction_b = ground_b - dem_b
    count = max(3, math.ceil(max(length_m, 1.0) / PROFILE_STEP_M) + 1)
    samples: list[float] = []
    for i in range(count):
        t = i / (count - 1)
        elevation = source.elevation(lat_a + (lat_b - lat_a) * t, lon_a + (lon_b - lon_a) * t)
        if elevation is None:
            return None  # never interpolate across a hole (data-model §lifecycle)
        samples.append(round(elevation + correction_a + (correction_b - correction_a) * t, 1))
    return LinkProfile(samples_amsl=samples)


def evaluate_clearance(
    profile: LinkProfile,
    *,
    height_a_amsl: float,
    height_b_amsl: float,
    length_m: float,
    frequency_ghz: float | None,
) -> Clearance:
    """The link's worst-case terrain verdict (research R3), computed once in core.

    Per sample at along-path distance d1 (d2 = D − d1): line-of-sight height is the linear
    chord; effective terrain adds the 4/3-earth curvature bulge d1·d2/(2·k·R); the local
    first-Fresnel radius is sqrt(λ·d1·d2/D) (zero without a resolved frequency — the LOS
    basis). Margin = LOS − effective terrain − 0.6·r; the verdict is the worst of the
    path's INTERIOR — the two endpoint samples anchor the rendered profile but are not
    obstruction candidates (r = 0 at the mast, and a radio whose 022-resolved height sits
    centimeters below its own site ground is a height-heuristic quirk, not terrain:
    observed live 2026-07-06 on a healthy 60 GHz link, margin −0.3 m at t = 0).
    """

    samples = profile.samples_amsl
    count = len(samples)
    basis = ClearanceBasis.FRESNEL if frequency_ghz else ClearanceBasis.LOS
    frequency_mhz = frequency_ghz * 1000.0 if frequency_ghz else None
    worst_margin = math.inf
    worst_fraction = 0.5
    intrudes_full_zone = False
    for i, ground in enumerate(samples):
        if i == 0 or i == count - 1:
            continue  # endpoints render, never judge (see docstring)
        t = i / (count - 1)
        d1 = t * length_m
        d2 = length_m - d1
        line_of_sight = height_a_amsl + (height_b_amsl - height_a_amsl) * t
        bulge = d1 * d2 / (2.0 * _EFFECTIVE_EARTH_K * _EARTH_RADIUS_M)
        effective = ground + bulge
        radius = (
            fresnel_local_radius_m(d1, d2, length_m, frequency_mhz) if frequency_mhz else 0.0
        )
        margin = line_of_sight - effective - _CORE_FACTOR * radius
        if line_of_sight - effective - radius < 0.0:
            intrudes_full_zone = True
        if margin < worst_margin:
            worst_margin = margin
            worst_fraction = t
    if worst_margin < 0.0:
        verdict = ClearanceVerdict.OBSTRUCTED
    elif intrudes_full_zone and basis is ClearanceBasis.FRESNEL:
        verdict = ClearanceVerdict.MARGINAL
    else:
        verdict = ClearanceVerdict.CLEAR
    return Clearance(
        verdict=verdict,
        margin_m=round(worst_margin, 1),
        at_fraction=round(worst_fraction, 3),
        basis=basis,
    )


# --- site grids + peaks (research R4/R5) --------------------------------------------------------


def grid_half_extent(enu: dict[str, tuple[float, float, float]]) -> float:
    """A site's grid half-extent: max endpoint reach + margin, clamped (research R4).

    Public since 098: the BRC city-grid step reuses it as the in-range gating radius —
    ONE definition of "how far this site can see" (098 research D6).
    """

    reach = 0.0
    for east, north, _up in enu.values():
        reach = max(reach, math.sqrt(east * east + north * north))
    return min(GRID_HALF_EXTENT_MAX_M, max(GRID_HALF_EXTENT_MIN_M, reach + GRID_MARGIN_M))


def build_site_grid(
    source: TerrainTileSource,
    *,
    latitude: float,
    longitude: float,
    enu: dict[str, tuple[float, float, float]],
) -> SiteTerrain | None:
    """The site's heightfield grid, or None if ANY post is missing (research R4).

    Heights are relative to the DEM's own value at the site origin (self-referenced: both
    terms share any datum offset, and the wireframe passes through the scene origin).
    """

    dem_origin = source.elevation(latitude, longitude)
    if dem_origin is None:
        return None
    half_extent = grid_half_extent(enu)
    step = 2.0 * half_extent / (GRID_POSTS - 1)
    cos_lat = math.cos(math.radians(latitude))
    heights: list[int] = []
    for i in range(GRID_POSTS):  # rows south → north
        north = -half_extent + i * step
        lat = latitude + math.degrees(north / _EARTH_RADIUS_M)
        for j in range(GRID_POSTS):  # west → east within a row
            east = -half_extent + j * step
            lon = longitude + math.degrees(east / (_EARTH_RADIUS_M * cos_lat))
            elevation = source.elevation(lat, lon)
            if elevation is None:
                return None
            heights.append(round((elevation - dem_origin) * 10.0))
    return SiteTerrain(half_extent_m=half_extent, posts=GRID_POSTS, rel_heights_dm=heights)


@dataclass
class Summit:
    """One gazetteer summit (name + coordinates); heights come from the DEM, not GNIS."""

    name: str
    latitude: float
    longitude: float


def peaks_for_site(
    source: TerrainTileSource,
    summits: list[Summit],
    *,
    latitude: float,
    longitude: float,
    half_extent_m: float,
) -> list[Peak]:
    """Named summits inside the site's grid extent, positioned in its ENU frame.

    Heights use the same DEM self-referenced rule as the grid so labels sit exactly on the
    wireframe (research R5); a summit whose elevation is unavailable is skipped (it could
    not be placed on the rendered terrain anyway).
    """

    dem_origin = source.elevation(latitude, longitude)
    if dem_origin is None:
        return []
    peaks: list[Peak] = []
    for summit in summits:
        east, north = enu_offset(latitude, longitude, summit.latitude, summit.longitude)
        if abs(east) > half_extent_m or abs(north) > half_extent_m:
            continue
        elevation = source.elevation(summit.latitude, summit.longitude)
        if elevation is None:
            continue
        peaks.append(
            Peak(
                name=summit.name,
                enu=(round(east, 2), round(north, 2), round(elevation - dem_origin, 2)),
            )
        )
    return sorted(peaks, key=lambda p: p.name)


# --- terrain shade underlay (feature 055, research R1–R5) ---------------------------------------

# The hillshade raster is DEM-native (~30 m/px) and generated ONCE per export from the durable
# tile cache; a sidecar meta file makes reuse the common path so the 5-minute cron never repeats
# the multi-million-sample raster (research R4). Every number is decided here — the widget only
# maps a per-site rectangle through its own view transform (Principle III).
SHADE_STEP_M = 30.0
SHADE_PARAMS_VERSION = 1  # bump ⇒ forced regeneration (shader/encoder changes)
SHADE_AZIMUTH_DEG = 315.0  # NW illumination (GDAL default)
SHADE_ALTITUDE_DEG = 45.0  # sun altitude ⇒ flat terrain shades to ~180
SHADE_ZFACTOR = 1.0
# Cap the shade bbox around the site centroid (Principle VI): a single far-flung / mis-located
# site (the controller lists non-link sites too) must not explode the raster into a continent-
# scale, hours-long generation. Same 28 km cap the per-site grids use — positions clamp to
# ±cap and each site's half-extent caps at the same value, so the bbox is bounded by ±2×cap
# worst case; a site outside the capped bbox simply gets no underlay rect (US4-6), never a
# misregistered one.
SHADE_MAX_HALF_EXTENT_M = GRID_HALF_EXTENT_MAX_M


@dataclass
class ShadePlan:
    """The georegistered grid a shade raster is sampled on (feature 055, research R2).

    ``origin`` is the site centroid; ``bbox_enu`` is the image rectangle in that origin frame —
    ``(e_min, n_min, e_max, n_max)`` — clamped around the centroid, expanded by the margin, and
    grown to a whole number of ``step_m`` pixels so registration is exact by construction.
    West→east = increasing col; north→south = row.
    """

    origin_lat: float
    origin_lon: float
    step_m: float
    width: int
    height: int
    e_min: float
    n_min: float
    e_max: float
    n_max: float

    @property
    def bbox_enu(self) -> tuple[float, float, float, float]:
        return (self.e_min, self.n_min, self.e_max, self.n_max)


@dataclass
class ShadeMetaFile:
    """A parsed ``terrain-shade.meta.json`` — regeneration-policy input, never served."""

    params_version: int
    origin_lat: float
    origin_lon: float
    step_m: float
    width: int
    height: int
    bbox_enu: tuple[float, float, float, float]
    generated_at: datetime


def plan_shade(
    extents: list[tuple[float, float, float]],
    *,
    step_m: float = SHADE_STEP_M,
    max_half_extent_m: float = SHADE_MAX_HALF_EXTENT_M,
) -> ShadePlan | None:
    """Plan the shade grid covering every site's terrain-grid square (research R2, Principle VI).

    ``extents`` are ``(lat, lon, half_extent_m)`` — each site's position plus the half-extent
    of its own 031 wireframe grid, so the underlay reaches exactly as far as the 3D terrain for
    whichever anchor the viewer picks (055 follow-up: the old sites-bbox + 2 km margin cut the
    raster ~26 km short of an edge site's wireframe). ``None`` when there is nothing to cover.
    The origin is the site CENTROID; each site's ENU offset is clamped to ``±max_half_extent_m``
    and its half-extent capped at the same value before the union is taken, so one far-flung /
    mis-located site cannot explode the raster into a continent-scale generation — it simply
    falls outside the covered rectangle (and later gets no rect). The rectangle is grown to a
    whole number of ``step_m`` pixels.
    """

    if not extents:
        return None
    origin_lat = sum(lat for lat, _lon, _half in extents) / len(extents)
    origin_lon = sum(lon for _lat, lon, _half in extents) / len(extents)
    e_lows: list[float] = []
    e_highs: list[float] = []
    n_lows: list[float] = []
    n_highs: list[float] = []
    for lat, lon, half_extent in extents:
        east, north = enu_offset(origin_lat, origin_lon, lat, lon)
        east = max(-max_half_extent_m, min(max_half_extent_m, east))
        north = max(-max_half_extent_m, min(max_half_extent_m, north))
        half = min(max_half_extent_m, half_extent)
        e_lows.append(east - half)
        e_highs.append(east + half)
        n_lows.append(north - half)
        n_highs.append(north + half)
    e_min = min(e_lows)
    n_min = min(n_lows)
    width = max(1, math.ceil((max(e_highs) - e_min) / step_m))
    height = max(1, math.ceil((max(n_highs) - n_min) / step_m))
    return ShadePlan(
        origin_lat=origin_lat,
        origin_lon=origin_lon,
        step_m=step_m,
        width=width,
        height=height,
        e_min=e_min,
        n_min=n_min,
        e_max=e_min + width * step_m,
        n_max=n_min + height * step_m,
    )


def sample_shade_elevations(
    source: TerrainTileSource, plan: ShadePlan
) -> list[list[float]] | None:
    """Bilinear elevations on the plan's ENU grid, row 0 = north; None if ANY sample missing.

    Refuses to guess (research R5, the 031 no-data rule): a single missing sample inside the
    bbox aborts the whole raster to the omission path — never a half-shaded image.
    """

    cos_lat = math.cos(math.radians(plan.origin_lat))
    grid: list[list[float]] = []
    for row in range(plan.height):
        north = plan.n_max - (row + 0.5) * plan.step_m
        lat = plan.origin_lat + math.degrees(north / _EARTH_RADIUS_M)
        line: list[float] = []
        for col in range(plan.width):
            east = plan.e_min + (col + 0.5) * plan.step_m
            lon = plan.origin_lon + math.degrees(east / (_EARTH_RADIUS_M * cos_lat))
            elevation = source.elevation(lat, lon)
            if elevation is None:
                return None
            line.append(elevation)
        grid.append(line)
    return grid


def hillshade_grid(elevations: list[list[float]], step_m: float) -> list[list[int]]:
    """Horn 3×3 hillshade (ESRI/GDAL formula, NW azimuth 315°, altitude 45°) → 0–255 gray.

    Interior cells use the 8-neighbor gradient; edge cells copy their nearest interior
    neighbor (research R5). Flat terrain ⇒ ~180 (the standard 45°-altitude flat value).
    """

    height = len(elevations)
    width = len(elevations[0]) if height else 0
    zenith = math.radians(90.0 - SHADE_ALTITUDE_DEG)
    azimuth = math.radians(360.0 - SHADE_AZIMUTH_DEG + 90.0)
    cos_zenith = math.cos(zenith)
    sin_zenith = math.sin(zenith)

    if width < 3 or height < 3:  # no interior to sample — a valid flat raster
        flat = max(0, min(255, round(255.0 * cos_zenith)))
        return [[flat] * width for _ in range(height)]

    def shade_at(row: int, col: int) -> int:
        a = elevations[row - 1][col - 1]
        b = elevations[row - 1][col]
        c = elevations[row - 1][col + 1]
        d = elevations[row][col - 1]
        f = elevations[row][col + 1]
        g = elevations[row + 1][col - 1]
        h = elevations[row + 1][col]
        i = elevations[row + 1][col + 1]
        dzdx = ((c + 2.0 * f + i) - (a + 2.0 * d + g)) / (8.0 * step_m)
        dzdy = ((g + 2.0 * h + i) - (a + 2.0 * b + c)) / (8.0 * step_m)
        slope = math.atan(SHADE_ZFACTOR * math.hypot(dzdx, dzdy))
        if dzdx != 0.0:
            aspect = math.atan2(dzdy, -dzdx)
            if aspect < 0.0:
                aspect += 2.0 * math.pi
        elif dzdy > 0.0:
            aspect = math.pi / 2.0
        elif dzdy < 0.0:
            aspect = 1.5 * math.pi
        else:
            aspect = 0.0
        value = 255.0 * (
            cos_zenith * math.cos(slope)
            + sin_zenith * math.sin(slope) * math.cos(azimuth - aspect)
        )
        return max(0, min(255, round(value)))

    interior = [[shade_at(r, c) for c in range(1, width - 1)] for r in range(1, height - 1)]
    grid: list[list[int]] = []
    for row in range(height):
        ir = min(max(row - 1, 0), height - 3)
        grid.append([interior[ir][min(max(col - 1, 0), width - 3)] for col in range(width)])
    return grid


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def encode_shade_png(grid: list[list[int]]) -> bytes:
    """Encode a 0–255 gray grid as an 8-bit truecolor RGB PNG (r=g=b), one IDAT (research R3).

    Stdlib-only (no Pillow); color type 2 so tests round-trip it through the existing
    ``decode_terrarium`` decoder. Filter type 0 on every scanline; zlib collapses the
    triplicated channels (the playa flat compresses hard).
    """

    height = len(grid)
    width = len(grid[0]) if height else 0
    raw = bytearray()
    for row in grid:
        raw.append(0)  # filter type 0 (None)
        for value in row:
            v = value & 0xFF
            raw += bytes((v, v, v))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def shade_registration(
    positions: dict[str, tuple[float, float]],
    *,
    origin_lat: float,
    origin_lon: float,
    bbox_enu: tuple[float, float, float, float],
) -> dict[str, tuple[float, float, float, float]]:
    """The shade image's rectangle in EACH covered site's ENU frame (research R2).

    ``positions`` maps site name → ``(lat, lon)``. The origin-frame rectangle ``bbox_enu`` is
    translated into each site's frame via ``enu_offset`` (flat-earth: the frames share axes,
    differ only by translation). A site outside the rectangle is OMITTED — its anchor gets no
    underlay rather than a misregistered one (US4-6, SC-004).
    """

    e_min, n_min, e_max, n_max = bbox_enu
    rects: dict[str, tuple[float, float, float, float]] = {}
    for name, (lat, lon) in positions.items():
        # the origin's position in this site's frame; a world point at origin-frame (e0,n0)
        # sits at site-frame (offset + (e0,n0)) since both frames share east/north axes.
        oe, on = enu_offset(lat, lon, origin_lat, origin_lon)
        se, sn = enu_offset(origin_lat, origin_lon, lat, lon)  # the site in the origin frame
        if not (e_min <= se <= e_max and n_min <= sn <= n_max):
            continue
        rects[name] = (
            round(oe + e_min, 2),
            round(on + n_min, 2),
            round(oe + e_max, 2),
            round(on + n_max, 2),
        )
    return rects


def read_shade_meta(path: Path) -> ShadeMetaFile | None:
    """Parse the sidecar meta, or None on any missing/corrupt field (self-heals by regen)."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        bbox = raw["bbox_enu"]
        return ShadeMetaFile(
            params_version=int(raw["params_version"]),
            origin_lat=float(raw["origin"]["lat"]),
            origin_lon=float(raw["origin"]["lon"]),
            step_m=float(raw["step_m"]),
            width=int(raw["width"]),
            height=int(raw["height"]),
            bbox_enu=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
            generated_at=datetime.fromisoformat(str(raw["generated_at"])),
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def shade_covers(
    meta: ShadeMetaFile,
    extents: list[tuple[float, float, float]],
    *,
    max_half_extent_m: float = SHADE_MAX_HALF_EXTENT_M,
) -> bool:
    """Whether every site's terrain-grid square falls inside the meta's bbox (research R4).

    ``extents`` are the same ``(lat, lon, half_extent_m)`` triples :func:`plan_shade` takes,
    clamped exactly as it clamps them, so a far-flung site that the raster deliberately
    excludes doesn't force a needless regeneration each export — it maps to the same clamped
    square the prior raster was planned against.
    """

    e_min, n_min, e_max, n_max = meta.bbox_enu
    for lat, lon, half_extent in extents:
        east, north = enu_offset(meta.origin_lat, meta.origin_lon, lat, lon)
        east = max(-max_half_extent_m, min(max_half_extent_m, east))
        north = max(-max_half_extent_m, min(max_half_extent_m, north))
        half = min(max_half_extent_m, half_extent)
        if not (
            e_min <= east - half
            and east + half <= e_max
            and n_min <= north - half
            and north + half <= n_max
        ):
            return False
    return True


def _meta_dict(plan: ShadePlan, generated_at: datetime) -> dict[str, object]:
    return {
        "params_version": SHADE_PARAMS_VERSION,
        "origin": {"lat": plan.origin_lat, "lon": plan.origin_lon},
        "step_m": plan.step_m,
        "width": plan.width,
        "height": plan.height,
        "bbox_enu": list(plan.bbox_enu),
        "generated_at": generated_at.isoformat(),
    }


def _reuse_meta(
    prior: ShadeMetaFile, named: dict[str, tuple[float, float]]
) -> TerrainShadeMeta:
    """Build the embedded doc meta (fresh rect table) from a reused prior raster."""

    return TerrainShadeMeta(
        generated_at=prior.generated_at,
        width=prior.width,
        height=prior.height,
        step_m=prior.step_m,
        sites=shade_registration(
            named,
            origin_lat=prior.origin_lat,
            origin_lon=prior.origin_lon,
            bbox_enu=prior.bbox_enu,
        ),
    )


def apply_terrain_shade(
    geometry: ArGeometry,
    *,
    tiles: TerrainTileSource,
    png_path: Path,
    meta_path: Path,
) -> tuple[TerrainShadeMeta | None, str, str | None]:
    """Reuse-or-regenerate the hillshade raster; return its embedded meta + status + reason.

    Never raises for expected failures (missing tiles/elevation): degrades to
    ``(meta-or-None, "omitted", reason)`` keeping any prior PNG (031 discipline). Reuse is the
    common path — a meta read + coverage check, no raster math. On reuse or a
    regeneration-failure-with-prior, the per-site rect table is STILL rebuilt fresh (the site
    set can change without the bbox growing). Atomic writes: PNG first, meta second — so a
    present meta always implies a valid PNG.
    """

    named = {site.name: (site.latitude, site.longitude) for site in geometry.sites}
    # 055 follow-up: each site brings the half-extent of its own 031 wireframe grid, so the
    # underlay's coverage matches the 3D terrain for whichever anchor the viewer picks.
    extents = [
        (site.latitude, site.longitude, grid_half_extent(site.enu))
        for site in geometry.sites
    ]
    if not extents:
        return None, "omitted", "no sites to cover"

    prior = read_shade_meta(meta_path)
    prior_ok = (
        prior is not None
        and prior.params_version == SHADE_PARAMS_VERSION
        and png_path.is_file()
    )

    if prior_ok:
        assert prior is not None
        if shade_covers(prior, extents):
            return _reuse_meta(prior, named), "reused", None

    plan = plan_shade(extents, step_m=SHADE_STEP_M)  # module global read at call time
    assert plan is not None  # extents is non-empty
    elevations = sample_shade_elevations(tiles, plan)
    if elevations is None:
        if prior_ok:
            assert prior is not None
            return (
                _reuse_meta(prior, named),
                "omitted",
                "terrain elevation unavailable for the shade bbox; kept prior raster",
            )
        return None, "omitted", "terrain elevation unavailable for the shade bbox"

    png = encode_shade_png(hillshade_grid(elevations, plan.step_m))
    generated_at = _utcnow()
    png_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_png = png_path.with_name(png_path.name + ".tmp")
    tmp_png.write_bytes(png)
    os.replace(tmp_png, png_path)  # PNG first...
    tmp_meta = meta_path.with_name(meta_path.name + ".tmp")
    tmp_meta.write_text(json.dumps(_meta_dict(plan, generated_at)), encoding="utf-8")
    os.replace(tmp_meta, meta_path)  # ...meta second (present ⇒ PNG valid)
    return (
        TerrainShadeMeta(
            generated_at=generated_at,
            width=plan.width,
            height=plan.height,
            step_m=plan.step_m,
            sites=shade_registration(
                named,
                origin_lat=plan.origin_lat,
                origin_lon=plan.origin_lon,
                bbox_enu=plan.bbox_enu,
            ),
        ),
        "generated",
        None,
    )


# --- the export-time terrain step (research R7) -------------------------------------------------


def apply_terrain(
    geometry: ArGeometry,
    *,
    tiles: TerrainTileSource,
    summits_by_state: dict[str, list[Summit] | None] | None = None,
) -> TerrainDocument:
    """Fill the geometry's clearance fields + omissions and build the TerrainDocument.

    Pure orchestration over the builders above: every exported link gets a profile +
    verdict or a named omission; every site a grid or a named omission; peaks per site
    from the provided per-state summit lists (None value = that state's gazetteer was
    unavailable; empty dict/None argument = peaks unconfigured). Never raises for missing
    data — omission entries are the degraded mode (FR-002).
    """

    endpoint_by_name = {endpoint.name: endpoint for endpoint in geometry.endpoints}
    site_by_name = {site.name: site for site in geometry.sites}
    document = TerrainDocument(generated_at=_utcnow())

    for link in geometry.links:
        end_a = endpoint_by_name.get(link.a)
        end_b = endpoint_by_name.get(link.b)
        site_a = site_by_name.get(end_a.site) if end_a else None
        site_b = site_by_name.get(end_b.site) if end_b else None
        if end_a is None or end_b is None or site_a is None or site_b is None:
            geometry.terrain_omitted.append(  # export invariant makes this unreachable
                TerrainOmission(item=link.name, reason="link endpoints not resolvable")
            )
            continue
        profile = build_profile(
            tiles,
            lat_a=end_a.latitude,
            lon_a=end_a.longitude,
            ground_a=site_a.ground_elevation_m,
            lat_b=end_b.latitude,
            lon_b=end_b.longitude,
            ground_b=site_b.ground_elevation_m,
            length_m=link.length_m,
        )
        if profile is None:
            geometry.terrain_omitted.append(
                TerrainOmission(item=link.name, reason="terrain data unavailable for the path")
            )
            continue
        document.profiles[link.id] = profile
        link.clearance = evaluate_clearance(
            profile,
            height_a_amsl=end_a.height_amsl_m,
            height_b_amsl=end_b.height_amsl_m,
            length_m=link.length_m,
            frequency_ghz=link.frequency_ghz,
        )

    all_summits: list[Summit] = []
    states_unavailable: list[str] = []
    if summits_by_state:
        for state, summits in sorted(summits_by_state.items()):
            if summits is None:
                states_unavailable.append(state)
            else:
                all_summits.extend(summits)

    for site in geometry.sites:
        grid = build_site_grid(
            tiles, latitude=site.latitude, longitude=site.longitude, enu=site.enu
        )
        if grid is None:
            geometry.terrain_omitted.append(
                TerrainOmission(
                    item=f"site {site.name}", reason="terrain data unavailable for the extent"
                )
            )
            continue
        grid.peaks = peaks_for_site(
            tiles,
            all_summits,
            latitude=site.latitude,
            longitude=site.longitude,
            half_extent_m=grid.half_extent_m,
        )
        document.sites[site.name] = grid

    if not summits_by_state:
        geometry.terrain_omitted.append(
            TerrainOmission(item="peaks", reason="no gnis_states configured")
        )
    elif states_unavailable:
        geometry.terrain_omitted.append(
            TerrainOmission(
                item="peaks",
                reason="gazetteer unavailable for: " + ", ".join(states_unavailable),
            )
        )
    return document
