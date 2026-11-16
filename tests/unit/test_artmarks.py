"""Unit (feature 126): the art-landmark dataset — load, nearest-within, operator fetch.

The dataset is the Burning Man public API's art array stored verbatim; answers only
ever read the local file (missing/corrupt ⇒ no references, never an error), and the
loaders and geometry are pure local-file reads, so no test touches the internet
(the dataset download itself is playa.fetch_dataset, pinned in test_playa.py).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from obi.core import artmarks
from obi.core.artmarks import ArtPiece, load_art, nearest_within

_LAT, _LON = 40.7761230, -114.8176930


def _feet_north(feet: float) -> float:
    return math.degrees(feet / 3.280839895 / 6_371_008.8)


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_reads_the_api_shape_and_skips_gpsless_entries(tmp_path: Path) -> None:
    path = tmp_path / "art.json"
    _write(
        path,
        [
            {
                "name": "Head Maze",
                "location": {"gps_latitude": _LAT, "gps_longitude": _LON},
                "location_string": "1:15 3900', Open Playa",
            },
            {"name": "Unplaced", "location": {"gps_latitude": None, "gps_longitude": None}},
            {"name": "No location at all"},
            {"name": "", "location": {"gps_latitude": 1.0, "gps_longitude": 2.0}},
            "junk entry",
        ],
    )
    assert load_art(path) == (ArtPiece("Head Maze", _LAT, _LON),)


def test_load_tolerates_top_level_gps_fields(tmp_path: Path) -> None:
    path = tmp_path / "art.json"
    _write(path, [{"name": "Fixture Piece", "gps_latitude": _LAT, "gps_longitude": _LON}])
    assert load_art(path) == (ArtPiece("Fixture Piece", _LAT, _LON),)


def test_missing_file_is_silently_empty(tmp_path: Path) -> None:
    assert load_art(tmp_path / "absent.json") == ()


def test_corrupt_or_nonlist_file_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "art.json"
    path.write_text("{ not json")
    assert load_art(path) == ()
    _write(path, {"not": "a list"})
    os.utime(path, (1.0, 2.0))  # distinct mtime so the memo can't mask the reparse
    assert load_art(path) == ()


def test_load_is_memoized_per_mtime(tmp_path: Path) -> None:
    path = tmp_path / "art.json"
    _write(path, [{"name": "First", "gps_latitude": _LAT, "gps_longitude": _LON}])
    os.utime(path, (1.0, 100.0))
    assert load_art(path)[0].name == "First"
    # Same mtime ⇒ the memo answers even though the bytes changed…
    _write(path, [{"name": "Second", "gps_latitude": _LAT, "gps_longitude": _LON}])
    os.utime(path, (1.0, 100.0))
    assert load_art(path)[0].name == "First"
    # …and a refetch (new mtime) shows up without any restart.
    os.utime(path, (1.0, 200.0))
    assert load_art(path)[0].name == "Second"


def test_nearest_within_boundary_both_sides() -> None:
    near = ArtPiece("Near", _LAT + _feet_north(200.0), _LON)
    far = ArtPiece("Far", _LAT + _feet_north(1000.0), _LON)
    assert nearest_within(_LAT, _LON, (near, far), 400.0) == near
    assert nearest_within(_LAT, _LON, (far,), 400.0) is None
    assert nearest_within(_LAT, _LON, (), 400.0) is None


def test_nearest_within_prefers_the_nearer_of_two() -> None:
    nearer = ArtPiece("Nearer", _LAT + _feet_north(150.0), _LON)
    close = ArtPiece("Close", _LAT + _feet_north(300.0), _LON)
    assert nearest_within(_LAT, _LON, (close, nearer), 400.0) == nearer


def test_module_cache_is_path_keyed(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _write(a, [{"name": "A", "gps_latitude": 1.0, "gps_longitude": 2.0}])
    _write(b, [{"name": "B", "gps_latitude": 3.0, "gps_longitude": 4.0}])
    assert load_art(a)[0].name == "A"
    assert load_art(b)[0].name == "B"
    assert artmarks.load_art(a)[0].name == "A"


def test_honorarium_preferred_over_a_nearer_selffunded_piece() -> None:
    # Operator directive 2026-09-02: honoraria tend to be larger and better known.
    fund = ArtPiece("Nearer Fund", _LAT + _feet_north(100.0), _LON)
    hono = ArtPiece("Big Honorarium", _LAT + _feet_north(300.0), _LON, honorarium=True)
    assert nearest_within(_LAT, _LON, (fund, hono), 400.0) == hono
    # …but an out-of-range honorarium never wins over an in-range piece.
    far_hono = ArtPiece("Far Honorarium", _LAT + _feet_north(1000.0), _LON, honorarium=True)
    assert nearest_within(_LAT, _LON, (fund, far_hono), 400.0) == fund


def test_program_field_marks_honoraria(tmp_path: Path) -> None:
    path = tmp_path / "art.json"
    _write(
        path,
        [
            {
                "name": "Granted",
                "program": "Honorarium",
                "gps_latitude": 1.0,
                "gps_longitude": 2.0,
            },
            {
                "name": "Self",
                "program": "Self-Funded",
                "gps_latitude": 1.0,
                "gps_longitude": 2.0,
            },
        ],
    )
    assert load_art(path) == (
        ArtPiece("Granted", 1.0, 2.0, honorarium=True),
        ArtPiece("Self", 1.0, 2.0),
    )
