"""Unit tests for rigid relocation (feature 134, T014)."""

from __future__ import annotations

import itertools

from obi.core.demo.relocate import Relocator, bearing_deg, centroid, haversine_m

POINTS = [
    (45.000, -100.000),
    (45.090, -100.000),  # ~10 km north
    (45.000, -99.873),  # ~10 km east
    (44.910, -100.120),
    (45.050, -100.150),
]
ANCHOR = (37.16, -118.27)


def test_pairwise_distances_preserved_within_half_metre() -> None:
    reloc = Relocator(centroid(POINTS), ANCHOR, 37.0)
    moved = [reloc.point(*p) for p in POINTS]
    for (a, b), (ma, mb) in zip(
        itertools.combinations(POINTS, 2), itertools.combinations(moved, 2), strict=True
    ):
        assert abs(haversine_m(*a, *b) - haversine_m(*ma, *mb)) < 0.5


def test_bearings_rotate_by_exactly_rotation() -> None:
    origin = centroid(POINTS)
    reloc = Relocator(origin, ANCHOR, 90.0)
    for p in POINTS:
        before = bearing_deg(*origin, *p)
        after = bearing_deg(*ANCHOR, *reloc.point(*p))
        assert abs(((after - before) % 360.0) - 90.0) < 0.01


def test_centroid_lands_on_anchor_and_far_from_origin() -> None:
    origin = centroid(POINTS)
    reloc = Relocator(origin, ANCHOR, 0.0)
    assert reloc.point(*origin) == ANCHOR
    for p in POINTS:
        assert haversine_m(*origin, *reloc.point(*p)) > 500_000


def test_rewrite_text_relocates_nearby_pairs_only_and_keeps_precision() -> None:
    reloc = Relocator(centroid(POINTS), ANCHOR, 0.0)
    out = reloc.rewrite_text("fix at 45.0123,-100.0456 and 45.01 -100.04; far 10.123456, 20.654321")
    assert "45.0123" not in out and "-100.0456" not in out
    assert "10.123456, 20.654321" in out
    lat_text = out.split("fix at ")[1].split(",")[0]
    assert len(lat_text.split(".")[1]) == 4
