"""Contract: the 078 XR-link-view vocabulary in bands.js actually COMPUTES correctly (R1–R10).

Every other bands assertion in this suite is an asset-source string match — which cannot catch an
off-by-one in a ramp interpolation or a mis-ordered precedence branch. The color ramps, the
signalT anchor math, the modeColor3d precedence table, coreScale, the pref round-trip/degrade, and
the link-list paging are pure logic; executing them in node (the bands_modes_headless.mjs harness)
is the only thing that proves the numbers, exactly as viewer2d_headless proves the map draw path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "bands_modes_headless.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_bands_mode_vocabulary_computes_per_contract() -> None:
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The harness prints one JSON summary line; a crash (missing export, syntax error) shows as a
    # non-zero exit with the stack on stderr — surface it verbatim.
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["target"] == "bands_modes"
    assert summary["failures"] == [], summary["failures"]
    assert summary["checks"] > 40  # every ramp/precedence/pref/paging assertion ran


def test_backdrop_decision_is_executed_not_just_grepped() -> None:
    """119 C8: the backdrop decision must stay EXECUTED, not merely asserted as source text.

    The failure this guards against is specific. `scene.background = THREE.Color(...)` renders
    nothing in an alpha-blend XR session (three.js overrides the clear color from the blend
    mode), so the opaque backdrop must be a texture. No test in this repo loads viewer.js, so
    that bug passes every text tripwire and surfaces only in the headset. The blend-mode gate
    and the per-backdrop terrain-style key are the parts that CAN run headlessly, and this
    keeps them running: a refactor that drops the harness block would otherwise be invisible.
    """
    source = HARNESS.read_text(encoding="utf-8")
    for helper in ("backdropAvailable", "terrainStyleKeyFor", "terrainStyleDefaultFor"):
        assert helper in source, f"{helper} is no longer exercised by the harness"

    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stdout}\n{proc.stderr}"
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["failures"] == [], summary["failures"]
    # The 119 block adds 24 checks on top of the pre-119 baseline of 96.
    assert summary["checks"] >= 120, (
        f"expected the backdrop checks to run (>=112), got {summary['checks']} — "
        "the harness block was probably removed or short-circuited"
    )
