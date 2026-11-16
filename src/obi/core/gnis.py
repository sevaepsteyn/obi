"""GNIS named summits for the AR terrain layer (feature 031) — fetched once, cached durably.

The USGS Geographic Names Information System publishes per-state "Domestic Names" files
(public domain) on the National Map staged-products bucket. obi downloads the configured
states' files **once**, keeps only the Summit-class features (name + coordinates), caches
them as small JSON files under the AR state dir, and discards the zip — every later export
works offline (Principle VI, the elevation/terrain cache shape).

Summit HEIGHTS deliberately do not come from GNIS (mixed vintages/units in the wild): the
terrain DEM is the height authority, so peak labels sit exactly on the wireframe
(research R5). The fetcher is injectable so tests run offline against a fixture file.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

from obi.core.terrain import Summit

logger = logging.getLogger("obi")

_GNIS_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames/DomesticNames/"
    "DomesticNames_{state}_Text.zip"
)

# The GNIS feature class marking a named summit (the landmark set for peak labels).
_SUMMIT_CLASS = "summit"

# A fetcher returns the DomesticNames zip bytes for a 2-letter state code, or None on failure.
GnisFetcher = Callable[[str], bytes | None]


def _default_fetcher(timeout: float) -> GnisFetcher:
    def fetch(state: str) -> bytes | None:
        url = _GNIS_URL.format(state=state)
        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            logger.debug("GNIS fetch failed for %s (%s): %s", state, url, exc)
            return None

    return fetch


def parse_domestic_names(data: bytes) -> list[Summit] | None:
    """Summit-class features from a DomesticNames zip, or None if the file is unusable.

    The zip carries one pipe-delimited text member; header names are matched
    case-insensitively (the GNIS modernization changed their case). A row with a missing
    name or unparsable coordinates is skipped, never invented.
    """

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = [name for name in archive.namelist() if name.lower().endswith(".txt")]
        if not names:
            return None
        text = archive.read(names[0]).decode("utf-8-sig", errors="replace")
    except (zipfile.BadZipFile, OSError):
        return None
    reader = csv.reader(io.StringIO(text), delimiter="|")
    try:
        header = [column.strip().lower() for column in next(reader)]
    except StopIteration:
        return None
    try:
        name_i = header.index("feature_name")
        class_i = header.index("feature_class")
        lat_i = header.index("prim_lat_dec")
        lon_i = header.index("prim_long_dec")
    except ValueError:
        return None  # not the DomesticNames layout we know — unusable, not guessable
    summits: list[Summit] = []
    for row in reader:
        if len(row) <= max(name_i, class_i, lat_i, lon_i):
            continue
        if row[class_i].strip().lower() != _SUMMIT_CLASS:
            continue
        name = row[name_i].strip()
        try:
            latitude = float(row[lat_i])
            longitude = float(row[lon_i])
        except ValueError:
            continue
        if not name or not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            continue
        summits.append(Summit(name=name, latitude=latitude, longitude=longitude))
    return summits


class GnisSource:
    """Durable-cached per-state summit lookup (one small JSON file per state)."""

    def __init__(
        self, cache_dir: Path, *, fetcher: GnisFetcher | None = None, timeout: float = 120.0
    ) -> None:
        self._cache_dir = cache_dir
        self._fetch = fetcher or _default_fetcher(timeout)

    def _cache_path(self, state: str) -> Path:
        return self._cache_dir / f"gnis-summits-{state}.json"

    def summits(self, state: str) -> list[Summit] | None:
        """The state's cached summits, fetching + caching once; None = unavailable.

        An unreadable cache file is treated as missing (refetched); a fetch failure with
        no cache is the degraded mode — the caller records the omission (FR-002).
        """

        state = state.strip().upper()
        path = self._cache_path(state)
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return [
                        Summit(
                            name=str(entry["name"]),
                            latitude=float(entry["latitude"]),
                            longitude=float(entry["longitude"]),
                        )
                        for entry in raw
                    ]
            except (OSError, ValueError, TypeError, KeyError):
                logger.warning("GNIS cache unreadable, refetching: %s", path)
        data = self._fetch(state)
        if data is None:
            return None
        summits = parse_domestic_names(data)
        if summits is None:
            logger.warning("GNIS file for %s did not match the DomesticNames layout", state)
            return None
        payload = [
            {"name": s.name, "latitude": s.latitude, "longitude": s.longitude} for s in summits
        ]
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:  # cache write failure degrades, never fails the export
            logger.warning("could not write GNIS cache %s: %s", path, exc)
        return summits
