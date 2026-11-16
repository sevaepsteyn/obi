"""Contract: kit.createPanel behaves per its contract in a real JS runtime (feature 076).

The panel primitive owns document-level keydown listeners, a scoped Escape guard, modal backdrop
dismissal, and the pop-out branch selection — logic a source grep cannot verify. This suite executes
the REAL kit.js in the node DOM-stub harness (``tests/web_harness/kit_panel_headless.mjs``), the
same pattern as the phy/ospf smoke tests, so the done-gate's pytest run exercises it too.

Skipped where node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "kit_panel_headless.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_create_panel_passes_its_behavior_contract() -> None:
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 25  # open/close/destroy, Esc truth table, modal, pop-out branches
