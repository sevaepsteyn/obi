"""Black Rock City addresses from coordinates (feature 111) — pure geometry, no I/O.

Turns a fix into the address the city actually speaks: the corner form ("6:15 & A") when
the fix sits on a street ring whose letter is CERTAIN, otherwise the dial form ("6:15 and
2,670 feet from the Man"), bearing-and-distance beyond the city, and raw coordinates when
no city model exists at all. Every numeric decision is made here, in tested pure code (the
``core/geo.py`` principle); the one caller (:mod:`obi.core.mesh`) memoizes the derived
model per process.

The model derives from the SAME 098 survey cache the XR city grid uses (``BrcSource``,
never fetching here): the 12:00 azimuth is fit from the named plaza centroids ("3:00 & G
Plaza" → the implied axis; measured 45.05° true for 2026, fallback constant 45.0), and
ring streets are found per query by ray-casting the street outlines from the golden spike
through the fix.

Two live 2026-08-28 discoveries are load-bearing here. A ray aimed exactly along a radial
street runs INSIDE the street corridor, parallel to its edges, and crosses nothing — ring
detection retries nudged bearings. And letters cannot be assigned by simple order: near
Center Camp an extra ring sits between Esplanade and B, so a letter is claimed only when
the ring count between plaza-anchored letters exactly matches the letter slots — a wrong
street name is worse than no street name, and the ambiguous span degrades to the dial
form.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from obi.core.brc import BrcSource
from obi.core.geo import enu_offset

_FT_PER_M = 3.280839895


#: Corner form applies within this distance of a named ring-street centerline.
_CORNER_FT = 60.0

#: A crossing pair wider than this is not the two edges of one street (meters).
_MAX_STREET_WIDTH_M = 25.0

#: A ring must sit this close (ft) to a plaza-anchored radius to claim that letter.
_ANCHOR_MATCH_FT = 40.0

#: Bearing nudges for the radial-corridor degeneracy (degrees, tried in order).
_NUDGES_DEG = (0.0, 0.5, -0.5)

#: Beyond the outermost surveyed ring plus this margin, the city grid stops applying.
_OUTER_MARGIN_FT = 500.0

#: Without street data, "the city" ends here (≈ the trash fence's reach from the Man).
_CITY_FALLBACK_RADIUS_FT = 8000.0

#: Distances at/over this (2 mi) read better in miles.
_MILES_AT_FT = 10_560.0

_PLAZA_NAME = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*&\s*([A-Z])\s+Plaza\b")

#: A named street whose NAME is a clock time is a radial avenue, not a corner candidate.
_TIME_NAME = re.compile(r"^\d{1,2}:\d{2}$")

#: Survey names → the names people say. "Rods Road" (the survey's Center Camp ring) is
#: called Esplanade on the air — operator decision 2026-08-30, the data itself stays true.
_STREET_DISPLAY = {"ESP": "Esplanade", "Rods Road": "Esplanade"}

#: How a ring letter is SPOKEN: the NATO phonetic alphabet (operator decision
#: 2026-09-03, after an on-air test of the official 2026 "Axis Mundi" names —
#: Ararat/Bodhi/Ceiba/Delphi/Eternal/Fulcrum/Great Oak/Heiau/Iroko/Jiba/Kundalini —
#: NATO letters are unambiguous on a working channel). Neither set exists in the
#: public API or the survey GIS (both speak bare letters; the keyhole road's
#: "Route 66" is the one named ring the GIS carries). Applied at DISPLAY time only —
#: ring arithmetic (_name_rings, _letter_after) stays letter-based.
STREET_LETTER_SPOKEN = {
    "A": "Alpha",
    "B": "Bravo",
    "C": "Charlie",
    "D": "Delta",
    "E": "Echo",
    "F": "Foxtrot",
    "G": "Golf",
    "H": "Hotel",
    "I": "India",
    "J": "Juliet",
    "K": "Kilo",
}


def street_display(name: str) -> str:
    """The spoken form of a street name — ring letters become NATO phonetic words."""

    return STREET_LETTER_SPOKEN.get(name, name)


#: A ring farther than this from the fix cannot be its block boundary (ft).
_BETWEEN_MAX_FT = 700.0

#: Beyond the outermost ring by more than this, the fix is outside the city (ft).
_NAMED_EDGE_MARGIN_FT = 500.0

_COMPASS = (
    "north",
    "northeast",
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
)

Segment = tuple[float, float, float, float]


@dataclass(frozen=True)
class CityModel:
    """The derived city: clock orientation, projected street edges, plaza letter anchors."""

    az12_deg: float
    orientation_source: str  # "plaza-fit" | "fallback"
    origin: tuple[float, float]  # 137: the survey origin (lat, lon), from [brc]
    segments: tuple[Segment, ...]  # street-outline edges in spike-frame ENU meters
    anchors: tuple[tuple[str, float], ...]  # (letter, radius_ft), sorted by radius
    # 111 field follow-on ("6:15 and Route 66"): the survey's NAMED centerlines
    # (street_lines.geojson), when cached. Authoritative names — Esplanade..K, Route 66,
    # Rods Road, the Center Camp geometry — replacing the count-consistency inference
    # wherever present; the outline ray-cast stays as the nameless fallback.
    named_streets: tuple[NamedStreet, ...] = ()


@dataclass(frozen=True)
class NamedStreet:
    """One named survey street, projected. ``is_ring`` separates annular streets (corner
    candidates: A, Route 66, …) from radial avenues (whose names are clock times the dial
    already expresses)."""

    name: str
    is_ring: bool
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class BrcAddress:
    """The human answer. ``text`` is complete on its own; the fields feed renderers."""

    form: str  # "corner" | "between" | "dial" | "bearing" | "raw"
    text: str
    clock: str | None = None
    distance_ft: int | None = None
    street: str | None = None


ModelLoader = Callable[[], CityModel | None]

_MODEL_CACHE: dict[str, CityModel] = {}


def model_loader_for(settings: Any) -> ModelLoader:
    """The 098 survey cache → a city model for a ``[brc]`` config, memoized per process.

    ``settings`` is a :class:`~obi.core.config.BrcSettings` or ``None``; with no ``[brc]``
    the loader always answers ``None`` (feature 137: no built-in city), and callers fall back
    to raw coordinates. The fetcher is pinned to "unavailable": a location answer must never
    wait on the internet (Principle VI) — warming the cache via the 098 export is an operator
    act, not a tool-time one.
    """

    if settings is None:
        return lambda: None

    def load() -> CityModel | None:
        from obi.core.config import default_web_dir  # runtime import: keeps config a leaf here

        source = BrcSource.from_settings(settings, default_web_dir(), fetcher=lambda name: None)
        key = f"{source.cache_dir}|{source.origin}|{settings.az12_deg}"
        if key not in _MODEL_CACHE:
            _MODEL_CACHE[key] = derive_city_model(source, az12_fallback=settings.az12_deg)
        return _MODEL_CACHE[key]

    return load


def no_model() -> CityModel | None:
    """The loader for an unconfigured city: always ``None`` (raw coordinates)."""

    return None


def _coordinate_arrays(geometry: object) -> list[list[object]]:
    """Every polyline-shaped coordinate array in one GeoJSON geometry (the brc.py shape)."""

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


def _project(pair: object, origin: tuple[float, float]) -> tuple[float, float] | None:
    if not isinstance(pair, list) or len(pair) < 2:
        return None
    try:
        lon, lat = float(pair[0]), float(pair[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    east, north = enu_offset(origin[0], origin[1], lat, lon)
    if math.hypot(east, north) > 15_000.0:  # brc.py's sanity radius: not city geometry
        return None
    return east, north


def _street_segments(
    collection: dict[str, object] | None, origin: tuple[float, float]
) -> tuple[Segment, ...]:
    segments: list[Segment] = []
    features = collection.get("features") if isinstance(collection, dict) else None
    for feature in features if isinstance(features, list) else []:
        if not isinstance(feature, dict):
            continue
        for coordinates in _coordinate_arrays(feature.get("geometry")):
            previous: tuple[float, float] | None = None
            for pair in coordinates:
                point = _project(pair, origin)
                if point is None:
                    previous = None
                    continue
                if previous is not None:
                    segments.append((previous[0], previous[1], point[0], point[1]))
                previous = point
    return tuple(segments)


def _plaza_fit(
    collection: dict[str, object] | None,
    origin: tuple[float, float],
) -> tuple[float | None, tuple[tuple[str, float], ...]]:
    """(fitted 12:00 azimuth, letter anchors) from the named plaza centroids."""

    implied: list[float] = []
    radii: dict[str, list[float]] = {}
    features = collection.get("features") if isinstance(collection, dict) else None
    for feature in features if isinstance(features, list) else []:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        name = properties.get("name") if isinstance(properties, dict) else None
        match = _PLAZA_NAME.match(name) if isinstance(name, str) else None
        if match is None:
            continue
        arrays = _coordinate_arrays(feature.get("geometry"))
        points = [
            p for pair in (arrays[0] if arrays else []) if (p := _project(pair, origin)) is not None
        ]
        if not points:
            continue
        east = sum(p[0] for p in points) / len(points)
        north = sum(p[1] for p in points) / len(points)
        radius_ft = math.hypot(east, north) * _FT_PER_M
        bearing = math.degrees(math.atan2(east, north)) % 360.0
        clock_hours = int(match.group(1)) + int(match.group(2)) / 60.0
        implied.append((bearing - (clock_hours - 12.0) * 30.0) % 360.0)
        radii.setdefault(match.group(3), []).append(radius_ft)
    if not implied:
        return None, ()
    sines = sum(math.sin(math.radians(a)) for a in implied)
    cosines = sum(math.cos(math.radians(a)) for a in implied)
    az12 = math.degrees(math.atan2(sines, cosines)) % 360.0
    anchors = tuple(
        sorted(
            ((letter, sum(values) / len(values)) for letter, values in radii.items()),
            key=lambda item: item[1],
        )
    )
    return az12, anchors


def _named_streets(
    collection: dict[str, object] | None, origin: tuple[float, float]
) -> tuple[NamedStreet, ...]:
    """The survey's named centerlines, projected and classified ring/radial."""

    streets: list[NamedStreet] = []
    features = collection.get("features") if isinstance(collection, dict) else None
    for feature in features if isinstance(features, list) else []:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        props = properties if isinstance(properties, dict) else {}
        name_raw = props.get("name")
        if not isinstance(name_raw, str) or not name_raw.strip():
            continue
        name = _STREET_DISPLAY.get(name_raw.strip(), name_raw.strip())
        kind = props.get("kind")
        is_ring = not (kind == "avenue" or _TIME_NAME.match(name) is not None)
        segments: list[Segment] = []
        for coordinates in _coordinate_arrays(feature.get("geometry")):
            previous: tuple[float, float] | None = None
            for pair in coordinates:
                point = _project(pair, origin)
                if point is None:
                    previous = None
                    continue
                if previous is not None:
                    segments.append((previous[0], previous[1], point[0], point[1]))
                previous = point
        if segments:
            streets.append(NamedStreet(name=name, is_ring=is_ring, segments=tuple(segments)))
    return tuple(streets)


def derive_city_model(source: BrcSource, *, az12_fallback: float = 45.0) -> CityModel:
    """The city model from the 098 survey cache — total, degrading, never fetching anew.

    (The caller passes a cache-warm ``BrcSource``; a file already cached is never
    re-fetched by contract, and an entirely cold cache simply degrades the model.)
    """

    az12, anchors = _plaza_fit(source.geojson("plazas.geojson"), source.origin)
    segments = _street_segments(source.geojson("street_outlines.geojson"), source.origin)
    named = _named_streets(source.geojson("street_lines.geojson"), source.origin)
    return CityModel(
        az12_deg=az12 if az12 is not None else az12_fallback,
        orientation_source="plaza-fit" if az12 is not None else "fallback",
        origin=source.origin,
        segments=segments,
        anchors=anchors,
        named_streets=named,
    )


def _nearest_on(segments: tuple[Segment, ...], px: float, py: float) -> tuple[float, float]:
    """(distance, radius-from-spike at the nearest point), meters, of a point to a polyline."""

    best = math.inf
    best_radius = 0.0
    for x1, y1, x2, y2 in segments:
        dx, dy = x2 - x1, y2 - y1
        length_sq = dx * dx + dy * dy
        t = (
            0.0
            if length_sq == 0.0
            else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
        )
        qx, qy = x1 + t * dx, y1 + t * dy
        distance = math.hypot(px - qx, py - qy)
        if distance < best:
            best = distance
            best_radius = math.hypot(qx, qy)
    return best, best_radius


def _ray_crossings(segments: tuple[Segment, ...], bearing_deg: float) -> list[float]:
    dx = math.sin(math.radians(bearing_deg))
    dy = math.cos(math.radians(bearing_deg))
    crossings: list[float] = []
    for x1, y1, x2, y2 in segments:
        rxs = dx * (y2 - y1) - dy * (x2 - x1)
        if abs(rxs) < 1e-12:
            continue
        t = (x1 * (y2 - y1) - y1 * (x2 - x1)) / rxs
        u = (x1 * dy - y1 * dx) / rxs
        if 0.0 <= u < 1.0 and t > 0.0:
            crossings.append(t)
    crossings.sort()
    return crossings


def _ring_centerlines_ft(segments: tuple[Segment, ...], bearing_deg: float) -> list[float]:
    """Street-ring centerline radii (ft) along the fix's bearing, degeneracy-nudged.

    A ray aimed straight down a radial street parallels its edges and crosses almost
    nothing (live discovery, 2026-08-28) — so try nudged bearings and keep the richest
    result. Crossings pair up as the two edges of one street; a "pair" wider than a
    street is a block boundary artifact and is skipped.
    """

    best: list[float] = []
    for nudge in _NUDGES_DEG:
        crossings = _ray_crossings(segments, (bearing_deg + nudge) % 360.0)
        centerlines: list[float] = []
        index = 0
        while index + 1 < len(crossings):
            near, far = crossings[index], crossings[index + 1]
            if far - near <= _MAX_STREET_WIDTH_M:
                centerlines.append((near + far) / 2.0 * _FT_PER_M)
                index += 2
            else:
                index += 1
        if len(centerlines) > len(best):
            best = centerlines
    return best


def _letter_after(letter: str) -> str:
    return chr(ord(letter) + 1)


def _letters_between(low: str, high: str) -> int:
    return max(0, ord(high) - ord(low) - 1)


def _name_rings(
    centerlines_ft: list[float], anchors: tuple[tuple[str, float], ...]
) -> dict[float, str]:
    """radius → letter, claiming a letter ONLY where the assignment is certain.

    Innermost ring is Esplanade. Each plaza-anchored letter names its nearest ring (within
    tolerance). Between fixed points, letters are assigned in order only when the ring
    count exactly matches the letter slots — otherwise that span stays unnamed (the
    Center Camp extra-ring lesson: a wrong street name is worse than none).
    """

    if not centerlines_ft:
        return {}
    ordered = sorted(centerlines_ft)
    named: dict[float, str] = {ordered[0]: "Esplanade"}

    anchor_rings: list[tuple[int, str]] = []  # (index into ordered, letter)
    for letter, radius_ft in anchors:
        nearest = min(range(len(ordered)), key=lambda i: abs(ordered[i] - radius_ft))
        if abs(ordered[nearest] - radius_ft) <= _ANCHOR_MATCH_FT and nearest > 0:
            named[ordered[nearest]] = letter
            anchor_rings.append((nearest, letter))
    anchor_rings.sort()

    # Espl(0) → first anchor: slots are A..(anchor-1).
    spans: list[tuple[int, int, str, str]] = []  # (low index, high index, low letter, high letter)
    if anchor_rings:
        first_index, first_letter = anchor_rings[0]
        spans.append((0, first_index, "A", first_letter))
        for (low_i, low_l), (high_i, high_l) in zip(anchor_rings, anchor_rings[1:], strict=False):
            spans.append((low_i, high_i, _letter_after(low_l), high_l))
    for low_index, high_index, start_letter, end_letter in spans:
        between = ordered[low_index + 1 : high_index]
        slots = (
            _letters_between(chr(ord(start_letter) - 1), end_letter)
            if start_letter != "A"
            else ord(end_letter) - ord("A")
        )
        if len(between) != slots:
            continue
        letter = start_letter
        for radius in between:
            named[radius] = letter
            letter = _letter_after(letter)
    # Beyond the last anchor: sequential letters outward.
    if anchor_rings:
        last_index, last_letter = anchor_rings[-1]
        letter = _letter_after(last_letter)
        for radius in ordered[last_index + 1 :]:
            named[radius] = letter
            letter = _letter_after(letter)
    return named


def _clock_text(bearing_deg: float, az12_deg: float) -> str:
    offset_hours = ((bearing_deg - az12_deg) % 360.0) / 30.0
    clock = 12.0 + offset_hours
    if clock >= 12.5:
        clock -= 12.0
    total_minutes = int(round(clock * 60.0 / 5.0)) * 5
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        hours = 12
    return f"{hours}:{minutes:02d}"


def _feet_text(distance_ft: float) -> tuple[int, str]:
    rounded = int(round(distance_ft / 10.0)) * 10
    return rounded, f"{rounded:,}"


def _clock_value(clock: str) -> float:
    """ "6:45" → 6.75; "12:00" → 0.0 (the deep-playa axis, outside the city's 2–10 arc)."""

    hours, minutes = clock.split(":")
    return int(hours) % 12 + int(minutes) / 60.0


def _compass(bearing_deg: float) -> str:
    return _COMPASS[int((bearing_deg + 22.5) % 360.0 // 45.0)]


def _miles_text(distance_ft: float) -> str:
    miles = distance_ft / 5280.0
    text = f"{miles:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _named_address(
    model: CityModel,
    east: float,
    north: float,
    clock: str,
    distance_ft: float,
    bearing: float,
) -> BrcAddress:
    """The address from named centerlines: corner → between → dial → bearing.

    On-a-street (≤ corner distance of a ring) names the corner. Mid-block inside the city
    names the block ("6:15, between Route 66 and A") — the field lesson behind this path:
    inside the city, streets beat raw distance. Inside Esplanade or past the outermost
    ring the streets stop meaning anything, so the dial/bearing forms take over.
    """

    rounded_ft, feet = _feet_text(distance_ft)
    fix_radius_ft = distance_ft

    rings: list[tuple[float, float, str]] = []  # (distance_ft, radius_at_nearest_ft, name)
    for street in model.named_streets:
        if not street.is_ring:
            continue
        distance_m, radius_m = _nearest_on(street.segments, east, north)
        rings.append((distance_m * _FT_PER_M, radius_m * _FT_PER_M, street.name))
    if not rings:
        return BrcAddress(
            form="dial",
            text=f"{clock} and {feet} feet from the Man",
            clock=clock,
            distance_ft=rounded_ft,
        )

    rings.sort()
    nearest_dist, _nearest_radius, nearest_name = rings[0]
    if nearest_dist <= _CORNER_FT:
        spoken = street_display(nearest_name)
        return BrcAddress(
            form="corner",
            text=f"{clock} & {spoken}",
            clock=clock,
            distance_ft=rounded_ft,
            street=spoken,
        )

    inner = [r for r in rings if r[1] < fix_radius_ft and r[0] <= _BETWEEN_MAX_FT]
    outer = [r for r in rings if r[1] > fix_radius_ft and r[0] <= _BETWEEN_MAX_FT]
    if inner and outer:
        inner_name, outer_name = inner[0][2], outer[0][2]
        if inner_name == outer_name:
            # One street's arcs on both sides of the fix (the Center Camp circle is not
            # concentric with the Man; renames can also merge streets): "between X and X"
            # is nonsense — the fix is simply at that street (field 2026-08-30, Tumble
            # playa-side of Esplanade near the keyhole).
            spoken = street_display(inner_name)
            return BrcAddress(
                form="corner",
                text=f"{clock} & {spoken}",
                clock=clock,
                distance_ft=rounded_ft,
                street=spoken,
            )
        return BrcAddress(
            form="between",
            text=(
                f"{clock}, between {street_display(inner_name)} and {street_display(outer_name)}"
            ),
            clock=clock,
            distance_ft=rounded_ft,
        )

    outermost_radius = max(r[1] for r in rings)
    if not outer and fix_radius_ft > outermost_radius + _NAMED_EDGE_MARGIN_FT:
        if distance_ft >= _MILES_AT_FT:
            body = f"{_miles_text(distance_ft)} miles"
        else:
            body = f"{feet} feet"
        return BrcAddress(
            form="bearing",
            text=f"{body} {_compass(bearing)} of the Man",
            distance_ft=rounded_ft,
        )

    # 126: between the Esplanade and the Man on the city's arc (2:00 through 10:00),
    # BOTH distances are honest and useful — that's where the art cars park (operator
    # directive 2026-09-02). Deep playa outside the arc has no Esplanade to measure
    # from, and a fix within corner distance of Esplanade already named the corner.
    esplanade = next((ring for ring in rings if ring[2] == "Esplanade"), None)
    if (
        esplanade is not None
        and fix_radius_ft < esplanade[1]
        and 2.0 <= _clock_value(clock) <= 10.0
    ):
        esp_feet = _feet_text(esplanade[0])[1]
        return BrcAddress(
            form="dial",
            text=(f"{clock}, {feet} feet from the Man and {esp_feet} feet from the Esplanade"),
            clock=clock,
            distance_ft=rounded_ft,
        )

    # Inside Esplanade (open playa / Man plaza) or an edge sliver: the dial says it best.
    return BrcAddress(
        form="dial",
        text=f"{clock} and {feet} feet from the Man",
        clock=clock,
        distance_ft=rounded_ft,
    )


def to_address(latitude: float, longitude: float, model: CityModel | None) -> BrcAddress:
    """One fix → the best honest BRC address the available model supports. Never raises."""

    if model is None:  # 137: no [brc] city configured — the fix as plain coordinates
        return BrcAddress(form="raw", text=f"{latitude:.5f}, {longitude:.5f}")

    east, north = enu_offset(model.origin[0], model.origin[1], latitude, longitude)
    distance_ft = math.hypot(east, north) * _FT_PER_M
    bearing = math.degrees(math.atan2(east, north)) % 360.0
    rounded_ft, feet = _feet_text(distance_ft)

    clock = _clock_text(bearing, model.az12_deg)

    # The survey's NAMED centerlines, when cached, answer authoritatively — real street
    # names ("6:15 & Route 66"), the mid-block "between" form, Center Camp geometry and
    # all. The nameless outline inference below remains the fallback.
    if model.named_streets:
        return _named_address(model, east, north, clock, distance_ft, bearing)

    centerlines = _ring_centerlines_ft(model.segments, bearing) if model.segments else []
    city_edge_ft = max(centerlines) + _OUTER_MARGIN_FT if centerlines else _CITY_FALLBACK_RADIUS_FT
    if distance_ft > city_edge_ft:
        if distance_ft >= _MILES_AT_FT:
            body = f"{_miles_text(distance_ft)} miles"
        else:
            body = f"{feet} feet"
        return BrcAddress(
            form="bearing",
            text=f"{body} {_compass(bearing)} of the Man",
            distance_ft=rounded_ft,
        )

    named = _name_rings(centerlines, model.anchors)
    if named:
        nearest_radius = min(named, key=lambda radius: abs(radius - distance_ft))
        if abs(nearest_radius - distance_ft) <= _CORNER_FT:
            street = named[nearest_radius]
            return BrcAddress(
                form="corner",
                text=f"{clock} & {street}",
                clock=clock,
                distance_ft=rounded_ft,
                street=street,
            )

    return BrcAddress(
        form="dial",
        text=f"{clock} and {feet} feet from the Man",
        clock=clock,
        distance_ft=rounded_ft,
    )
