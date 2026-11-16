"""Contract: the LINK detail window actually RENDERS a fixture end-to-end (058).

The other LINK assertions in test_web_workspace.py are asset-source string matches — they can't
catch a runtime failure in mount()/render()/drawProfile(). This suite executes the REAL widget in
node against a fixture links.json + terrain.json (the DOM-stub harness in
tests/web_harness/link_headless.mjs): it resolves the link by its endpoint-pair key, renders the
detail fields + the elevation profile, and — for an unknown key — shows the honest absence
message instead of throwing. Synthetic fixtures only; skipped where node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "link_headless.mjs"
LINK_JS = STATIC_DIR / "widgets" / "link.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# The key the widget resolves against — the normalized, SORTED endpoint-identity pair (both entry
# points produce it via bands.js linkKey, so the window always receives the sorted form).
LINK_KEY = "basswood-ptp-tamarack-60~tamarack-ptp-basswood-60"


def _write_fixture(fixture_dir: Path) -> None:
    geometry = {
        "generated_at": "2026-07-12T00:00:00+00:00",
        "controller": "https://uisp.example.com",
        "sites": [{"name": "Tamarack", "enu": {}}, {"name": "Basswood", "enu": {}}],
        "endpoints": [
            {"name": "tamarack-ptp-basswood-60", "site": "Tamarack", "height_amsl_m": 1240.0},
            {"name": "basswood-ptp-tamarack-60", "site": "Basswood", "height_amsl_m": 1660.0},
        ],
        "links": [
            {
                "id": "L-60", "name": "Tamarack - Tower <-> Basswood Relay - West (60 GHz)",
                # endpoints stored in the OPPOSITE order to the key's sort — resolve must normalize
                "a": "basswood-ptp-tamarack-60", "b": "tamarack-ptp-basswood-60",
                "band": "60 GHz", "health": "healthy",
                "frequency_ghz": 60.48, "frequency_provenance": "link_channel",
                "length_m": 14464.0, "signal_dbm": -61,
                "fresnel_radius_m": 3.1,
                "clearance_verdict": "clear", "clearance_margin_m": 12.4,
                "clearance_at_fraction": 0.42,
                "capacity_down_bps": 1_751_750_000, "capacity_up_bps": 1_751_750_000,
                "usage_down_bps": 40_000_000, "usage_up_bps": 20_000_000,
                "maintenance": False,
            },
        ],
        "excluded": [], "degraded": [], "terrain_omitted": [],
    }
    terrain = {
        "generated_at": "2026-07-12T00:00:00+00:00",
        "profiles": {"L-60": {"samples_amsl": [1240.0, 1300.0, 1280.0, 1500.0, 1660.0]}},
        "sites": {},
    }
    (fixture_dir / "links.json").write_text(json.dumps(geometry), encoding="utf-8")
    (fixture_dir / "terrain.json").write_text(json.dumps(terrain), encoding="utf-8")


def _render(fixture_dir: Path, key: str) -> dict:
    proc = subprocess.run(
        ["node", str(HARNESS), str(LINK_JS), str(fixture_dir), key],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_link_window_renders_the_resolved_link(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    out = _render(tmp_path, LINK_KEY)

    assert out["mountError"] is None, out["mountError"]
    # resolved despite the reversed a/b order (the key is order-independent) — title set to the name
    assert out["name"] == "Tamarack - Tower <-> Basswood Relay - West (60 GHz)"
    assert out["health"] == "healthy"
    # A, B, band, frequency, length, signal, capacity, usage, clearance = the panel's field rows
    assert out["rows"] >= 8
    assert out["message"] is None  # a resolved link shows fields, not a fallback message
    assert out["profile"] is True  # the elevation profile drew (terrain profile present by link id)


def test_link_window_reports_an_unknown_key_without_throwing(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    out = _render(tmp_path, "ghost-ptp-nowhere-60~nowhere-ptp-ghost-60")

    assert out["mountError"] is None, out["mountError"]
    assert out["rows"] == 0
    assert out["message"] == "this link is not in the current export"
