"""Rigid relocation of the network to the demo region (feature 134, FR-012, research R1).

Every coordinate is re-expressed as (great-circle distance, initial bearing) from the real
network's centroid, the bearing rotated by ``rotation_deg``, and the same distance walked from
the destination anchor. That is a rotation of the sphere, so every pairwise distance and every
relative bearing survives exactly — link lengths, mast heights (untouched) and site layout are
preserved while no absolute real coordinate remains. The centroid and the rotation live only in
the private mapping; the manifest names the destination region, never the source.
"""

from __future__ import annotations

import math
import re

_EARTH_RADIUS_M = 6_371_000.0

#: A decimal ``lat, lon`` / ``lat lon`` pair in free text (≥ 3 decimals so integers and short
#: decimals never match).
COORD_PAIR = re.compile(r"(?<![\d.])(-?\d{1,2}\.\d{3,})(\s*,\s*|\s+)(-?\d{1,3}\.\d{3,})(?![\d.])")

#: Free-text pairs farther than this from the real centroid are not the network's coordinates.
_TEXT_RADIUS_M = 300_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def destination(lat: float, lon: float, bearing: float, distance_m: float) -> tuple[float, float]:
    p1, l1 = math.radians(lat), math.radians(lon)
    theta, delta = math.radians(bearing), distance_m / _EARTH_RADIUS_M
    p2 = math.asin(
        math.sin(p1) * math.cos(delta) + math.cos(p1) * math.sin(delta) * math.cos(theta)
    )
    l2 = l1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(p1),
        math.cos(delta) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """The mean of ``points`` (adequate for a network a few tens of km across)."""

    if not points:
        raise ValueError("no coordinates to relocate")
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


class Relocator:
    """Move coordinates from around ``origin`` to around ``anchor``, rotated (an isometry)."""

    def __init__(
        self, origin: tuple[float, float], anchor: tuple[float, float], rotation_deg: float
    ) -> None:
        self.origin = origin
        self.anchor = anchor
        self.rotation_deg = rotation_deg

    def point(self, lat: float, lon: float) -> tuple[float, float]:
        distance = haversine_m(self.origin[0], self.origin[1], lat, lon)
        if distance < 1e-6:
            return self.anchor
        bearing = bearing_deg(self.origin[0], self.origin[1], lat, lon)
        return destination(
            self.anchor[0], self.anchor[1], (bearing + self.rotation_deg) % 360.0, distance
        )

    def near_origin(self, lat: float, lon: float, radius_m: float = _TEXT_RADIUS_M) -> bool:
        return haversine_m(self.origin[0], self.origin[1], lat, lon) <= radius_m

    def rewrite_text(self, text: str) -> str:
        """Relocate every decimal coordinate pair near the network; keep each value's precision."""

        if "." not in text:
            return text
        return COORD_PAIR.sub(self._sub, text)

    def _sub(self, m: re.Match[str]) -> str:
        lat_text, sep, lon_text = m.group(1), m.group(2), m.group(3)
        lat, lon = float(lat_text), float(lon_text)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or not self.near_origin(lat, lon):
            return m.group(0)
        new_lat, new_lon = self.point(lat, lon)
        lat_dp = len(lat_text.split(".")[1])
        lon_dp = len(lon_text.split(".")[1])
        return f"{new_lat:.{lat_dp}f}{sep}{new_lon:.{lon_dp}f}"
