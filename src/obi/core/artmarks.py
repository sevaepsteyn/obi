"""Art-installation landmarks (feature 126): a local art dataset → ", near <name>".

``load_art`` reads the Burning Man public API's art array VERBATIM from the
operator-downloaded file (``obi playa fetch`` writes it since 127) and keeps the pieces with
published GPS coordinates. Answer paths call it on every render: the parse is memoized
per ``(path, mtime)``, so a refetch shows up without restarts and an unchanged file is
never re-read. Any trouble — no file, unreadable, malformed — yields ``()`` and the
answer simply carries no reference: a location answer never breaks, waits, or reaches
the internet on account of decoration (Principle VI). This module has NO network path
at all since 127 — the dataset download lives in :func:`obi.core.playa.fetch_dataset`
(`obi playa fetch`).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from obi.core.geo import enu_offset

logger = logging.getLogger("obi")

_FT_PER_M = 3.280839895


@dataclass(frozen=True)
class ArtPiece:
    """One published art installation — the name an answer may speak, and where it is.

    ``honorarium`` mirrors the dataset's program field: honorarium (Burning Man-granted)
    pieces tend to be larger and better known, so references prefer them (operator
    directive 2026-09-02).
    """

    name: str
    latitude: float
    longitude: float
    honorarium: bool = False


_CACHE: dict[str, tuple[float, tuple[ArtPiece, ...]]] = {}


def load_art(path: Path) -> tuple[ArtPiece, ...]:
    """The dataset's GPS-bearing pieces; ``()`` on any trouble, memoized per mtime."""

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ()
    key = str(path)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.debug("artmarks: unreadable art dataset at %s", path, exc_info=True)
        raw = []
    pieces = _pieces_from(raw)
    _CACHE[key] = (mtime, pieces)
    return pieces


def _pieces_from(raw: object) -> tuple[ArtPiece, ...]:
    """GPS-bearing pieces from a decoded API art array; junk entries are skipped.

    Coordinates live under ``location.gps_latitude``/``gps_longitude`` in the API's
    format; top-level fields are tolerated for hand-made fixtures.
    """

    if not isinstance(raw, list):
        logger.debug("artmarks: art dataset is not a list — ignoring it")
        return ()
    pieces: list[ArtPiece] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        location = entry.get("location")
        source = location if isinstance(location, dict) else entry
        lat = source.get("gps_latitude")
        lon = source.get("gps_longitude")
        if not name or isinstance(lat, bool) or isinstance(lon, bool):
            continue
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        program = str(entry.get("program") or "")
        pieces.append(
            ArtPiece(
                name=name,
                latitude=float(lat),
                longitude=float(lon),
                honorarium=program.strip().lower().startswith("honorari"),
            )
        )
    return tuple(pieces)


def nearest_within(
    latitude: float, longitude: float, pieces: tuple[ArtPiece, ...], radius_ft: float
) -> ArtPiece | None:
    """The single best reference within ``radius_ft`` of the point, else ``None``.

    The nearest HONORARIUM piece wins over any nearer self-funded one (they tend to be
    larger and better known); with no honorarium in range, the nearest piece of any
    kind. Flat-earth meters via :func:`obi.core.geo.enu_offset` — the city model's own
    arithmetic, accurate to inches at playa scale.
    """

    best: ArtPiece | None = None
    best_key = (2, 0.0)
    for piece in pieces:
        east, north = enu_offset(latitude, longitude, piece.latitude, piece.longitude)
        distance_ft = math.hypot(east, north) * _FT_PER_M
        if distance_ft > radius_ft:
            continue
        key = (0 if piece.honorarium else 1, distance_ft)
        if best is None or key < best_key:
            best, best_key = piece, key
    return best
