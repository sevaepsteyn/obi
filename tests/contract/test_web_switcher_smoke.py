"""Contract: the window switcher behaves per its contract in a real JS runtime (feature 077).

`wm.list()` / `wm.activate()` are where this feature's two traps live, and neither is visible to a
source grep: `focus()` never un-hides a minimized window (so a focus-only `activate` is a silent
no-op), and `serialize()` drops ephemeral windows (so sourcing the list there omits chat/snapshot
windows). This suite executes the REAL wm.js in the node DOM-stub harness
(``tests/web_harness/wm_switcher_headless.mjs``), the same pattern as the panel/phy/ospf smoke
tests, so the done-gate's pytest run exercises it too.

See ``specs/077-shell-chrome/contracts/window-switcher.md`` for the contract itself.

Skipped where node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "wm_switcher_headless.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_window_switcher_passes_its_behavior_contract() -> None:
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 30  # list shape/order/completeness/liveness + activate's branches
