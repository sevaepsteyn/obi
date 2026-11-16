"""The frozen demo clock (feature 136, T015). Skipped where node is absent."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "demo_clock.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_demo_clock() -> None:
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR / "demo" / "clock.js")],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["nowDelta"] < 2000 and out["newDateDelta"] < 2000
    for key in ("explicitUnaffected", "parseWorks", "utcWorks", "instanceofOld", "instanceofNew",
                "callReturnsString", "isoInDemoYear", "realWasLater", "restored"):  # fmt: skip
        assert out[key] is True, key
