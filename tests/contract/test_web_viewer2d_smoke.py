"""Contract: the 2D MAP widget actually RENDERS a fixture end-to-end (055, FR-015).

Every other viewer2d assertion in this suite is an asset-source string match — which cannot
catch a runtime-only failure in draw(). This suite once shipped exactly that class of bug on the
OSPF map (a one-line map that every grep and `node --check` passed); the terrain underlay, the
utilization fill, and the parallel curves are all new draw-path code, so they get the same
executed-in-node treatment (tests/web_harness/viewer2d_headless.mjs). Synthetic fixtures only —
no live export.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "viewer2d_headless.mjs"
VIEWER2D = STATIC_DIR / "viewer2d.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _endpoint(name: str, site: str) -> dict:
    return {"name": name, "site": site, "maintenance": False}


def _link(link_id: str, a: str, b: str, capacity: int, usage: int | None) -> dict:
    link = {
        "id": link_id,
        "name": f"{a} <-> {b}",
        "a": a,
        "b": b,
        "band": "60 GHz",
        "health": "healthy",
        "capacity_down_bps": capacity,
        "capacity_up_bps": capacity,
    }
    if usage is not None:
        link["usage_down_bps"] = usage
        link["usage_up_bps"] = usage
    return link


def _write_fixture(fixture_dir: Path) -> None:
    """A live-shaped export with two co-sited parallel site-pairs (×2), a lone link, and both
    usage-bearing and usage-null links; terrain.json carries a shade block + a peak."""

    # co-sited radios share ENU coordinates (why the parallels must curve to be visible)
    enu = {
        "ranch1a": [0.0, 0.0, 10.0], "ranch2a": [0.0, 0.0, 10.0], "ranch3": [0.0, 0.0, 10.0],
        "tower1b": [2000.0, 0.0, 10.0], "tower2b": [2000.0, 0.0, 10.0],
        "relay1a": [0.0, 2000.0, 10.0], "relay2a": [0.0, 2000.0, 10.0],
        "relay3": [0.0, 2000.0, 10.0],
        "west1b": [2000.0, 2000.0, 10.0], "west2b": [2000.0, 2000.0, 10.0],
    }
    endpoints = [
        _endpoint("ranch1a", "Chestnut"),
        _endpoint("ranch2a", "Chestnut"),
        _endpoint("ranch3", "Chestnut"),
        _endpoint("tower1b", "Tower"),
        _endpoint("tower2b", "Tower"),
        _endpoint("relay1a", "Relay"),
        _endpoint("relay2a", "Relay"),
        _endpoint("relay3", "Relay"),
        _endpoint("west1b", "West"),
        _endpoint("west2b", "West"),
    ]
    links = [
        _link("l1", "ranch1a", "tower1b", 1_000_000_000, 500_000_000),  # pair 1, usage
        # pair 1's sibling is stored in the OPPOSITE a/b order — exactly how the live
        # 60/70 GHz Tamarack↔Mountain pair arrives. This once flipped its curve normal and
        # made both members bow to the SAME side (an exact overlap; 057 hotfix).
        _link("l2", "tower2b", "ranch2a", 1_000_000_000, None),          # pair 1, usage-null
        _link("l3", "relay1a", "west1b", 1_000_000_000, 900_000_000),    # pair 2, usage
        _link("l4", "relay2a", "west2b", 1_000_000_000, None),           # pair 2, usage-null
        _link("l5", "ranch3", "relay3", 1_000_000_000, 100_000_000),     # lone link, usage
    ]
    geometry = {
        "generated_at": "2026-07-12T00:00:00+00:00",
        "controller": "https://uisp.example.com",
        "sites": [{
            "name": "Chestnut", "latitude": 40.9, "longitude": -114.7,
            "ground_elevation_m": 1190.0, "enu": enu,
        }],
        "endpoints": endpoints, "links": links,
        "excluded": [], "degraded": [], "terrain_omitted": [],
    }
    terrain = {
        "generated_at": "2026-07-12T00:00:00+00:00",
        "profiles": {},
        "sites": {"Chestnut": {
            "half_extent_m": 4000.0, "posts": 3, "rel_heights_dm": [0] * 9,
            "peaks": [{"name": "Old Razorback", "enu": [500.0, 500.0, 50.0]}],
        }},
        "shade": {
            "generated_at": "2026-07-12T00:00:00+00:00",
            "width": 100, "height": 80, "step_m": 30.0,
            "sites": {"Chestnut": [-1500.0, -1500.0, 1500.0, 1500.0]},
        },
    }
    (fixture_dir / "links.json").write_text(json.dumps(geometry), encoding="utf-8")
    (fixture_dir / "terrain.json").write_text(json.dumps(terrain), encoding="utf-8")


def _render(fixture_dir: Path, *, terrain: bool) -> dict:
    argv = ["node", str(HARNESS), str(VIEWER2D), str(fixture_dir)]
    if terrain:
        argv.append("terrain")
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _chord_groups(segs: list[list[float]]) -> dict[tuple, set[tuple]]:
    """Distinct curve control points per unordered chord (stroke+fill repeats dedupe away)."""

    groups: dict[tuple, set[tuple]] = {}
    for ax, ay, cx, cy, bx, by in segs:
        key = tuple(sorted(((round(ax), round(ay)), (round(bx), round(by)))))
        groups.setdefault(key, set()).add((round(cx, 1), round(cy, 1)))
    return groups


def _canonical_side(key: tuple, control: tuple) -> float:
    """Signed side of the chord (in its CANONICAL sorted orientation) a control point sits on."""

    (ax, ay), (bx, by) = key
    return (bx - ax) * (control[1] - ay) - (by - ay) * (control[0] - ax)


def test_map_renders_curves_fills_and_underlay(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    off = _render(tmp_path, terrain=False)
    on = _render(tmp_path, terrain=True)

    # No mount/draw error in either configuration (the one-line-map regression left mountError set
    # and the tallies near zero).
    assert off["mountError"] is None, off["mountError"]
    assert on["mountError"] is None, on["mountError"]

    # Parallel separation (US1): the four bowed members of the two co-sited pairs each drew as a
    # quadratic curve; the lone link stays straight. A draw() abort would leave this near zero.
    assert off["curves"] >= 4
    assert on["curves"] == off["curves"]  # geometry is identical; only the underlay differs

    # Opposite-side regression (057 hotfix): l2 is stored with reversed a/b order, which used to
    # flip its normal so both pair-1 curves bowed to the SAME side — an exact overlap. Every
    # parallel pair must bow to opposite sides of its (canonically oriented) chord.
    pairs = {k: v for k, v in _chord_groups(off["curveSegs"]).items() if len(v) >= 2}
    assert len(pairs) == 2, f"expected both fixture pairs to draw two distinct curves: {pairs}"
    for key, controls in pairs.items():
        sides = [_canonical_side(key, control) for control in controls]
        assert len(sides) == 2 and sides[0] * sides[1] < 0, (
            f"parallel curves bow to the same side of chord {key}: {sorted(controls)}"
        )

    # Utilization fill (US2): exactly the three usage-bearing visible links drew an inner fill
    # stroke (its distinct half-alpha ink); the two usage-null links drew none (absence ≠ zero).
    assert off["fillStrokes"] == 3
    assert on["fillStrokes"] == 3

    # Terrain underlay (US4): drawImage fires ONLY when the toggle was pre-enabled.
    assert off["drawImages"] == 0
    assert on["drawImages"] >= 1

    # Labels drew (capacity readouts + site labels; a peak name when terrain is on).
    assert off["texts"] > 4
    assert on["texts"] > off["texts"]  # the peak marker's name is the extra text
