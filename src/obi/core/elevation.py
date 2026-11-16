"""Per-site ground elevation for the AR geometry (feature 022) — USGS EPQS, baked once.

Sites in UISP carry no altitude, so ground elevation comes from the public USGS Elevation
Point Query Service — fetched **once per site** and cached durably (terrain is static), so
every later export works with zero internet (FR-005, Principle VI). The resolution order is:

1. operator ``[ar] ground_overrides`` entry for the site name (always wins) → ``override``
2. the durable cache → ``cache``
3. a live EPQS fetch (cached on success) → ``epqs``
4. the configured default ground elevation → ``default_ground`` (degraded; surfaced in the
   export summary, never a failure)

The fetcher is injectable so tests run offline against the recorded EPQS fixture. This module
is the feature's ONLY network I/O besides the controller reads.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx

from obi.core.models import GroundProvenance

logger = logging.getLogger("obi")

_EPQS_URL = "https://epqs.nationalmap.gov/v1/json"

# Elevations outside this band are treated as EPQS no-data sentinels / nonsense.
_PLAUSIBLE_MIN_M = -430.0  # below the Dead Sea shore ⇒ not terrain
_PLAUSIBLE_MAX_M = 9000.0

# A fetcher returns the ground elevation in meters AMSL for (lat, lon), or None on any failure.
Fetcher = Callable[[float, float], float | None]


def _parse_epqs_value(payload: object) -> float | None:
    """The elevation from an EPQS response body. Note ``value`` is a JSON **string** live."""

    if not isinstance(payload, dict):
        return None
    try:
        value = float(str(payload.get("value")).strip())
    except (TypeError, ValueError):
        return None
    if not (_PLAUSIBLE_MIN_M <= value <= _PLAUSIBLE_MAX_M):
        return None
    return value


def _default_fetcher(timeout: float) -> Fetcher:
    def fetch(lat: float, lon: float) -> float | None:
        try:
            response = httpx.get(
                _EPQS_URL,
                params={"x": lon, "y": lat, "units": "Meters", "wkid": 4326},
                timeout=timeout,
            )
            response.raise_for_status()
            return _parse_epqs_value(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("EPQS lookup failed for %.5f,%.5f: %s", lat, lon, exc)
            return None

    return fetch


class ElevationSource:
    """Durable-cached ground-elevation lookup (one JSON file under obi's state dir)."""

    def __init__(
        self,
        cache_path: Path,
        *,
        overrides: dict[str, float] | None = None,
        default_ground_elevation: float,
        fetcher: Fetcher | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._cache_path = cache_path
        self._overrides = dict(overrides or {})
        self._default = default_ground_elevation
        self._fetch = fetcher or _default_fetcher(timeout)
        self._cache = self._load_cache()

    def _load_cache(self) -> dict[str, float]:
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        cache: dict[str, float] = {}
        if isinstance(raw, dict):
            for key, entry in raw.items():
                value = entry.get("elevation_m") if isinstance(entry, dict) else entry
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cache[str(key)] = float(value)
        return cache

    def _save_cache(self) -> None:
        payload = {key: {"elevation_m": value} for key, value in sorted(self._cache.items())}
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:  # a cache write failure degrades, never fails the export
            logger.warning("could not write elevation cache %s: %s", self._cache_path, exc)

    @staticmethod
    def _key(lat: float, lon: float) -> str:
        return f"{lat:.5f},{lon:.5f}"

    def ground(self, site_name: str, lat: float, lon: float) -> tuple[float, GroundProvenance]:
        """The site's ground elevation (m AMSL) + provenance, per the resolution order above.

        Matches the :data:`obi.core.geo.GroundLookup` shape the geometry builder consumes.
        """

        if site_name in self._overrides:
            return self._overrides[site_name], GroundProvenance.OVERRIDE
        key = self._key(lat, lon)
        if key in self._cache:
            return self._cache[key], GroundProvenance.CACHE
        elevation = self._fetch(lat, lon)
        if elevation is not None:
            self._cache[key] = elevation
            self._save_cache()
            return elevation, GroundProvenance.EPQS
        return self._default, GroundProvenance.DEFAULT_GROUND
