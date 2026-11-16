"""Contract: the OSPF map widget actually RENDERS a fixture snapshot end-to-end (054).

Every other widget assertion in this suite is an asset-source string match — which cannot catch
a runtime-only failure. This suite once shipped exactly that: draw() referenced an undefined
constant, aborted after the first edge stroke, the widget's poll try/catch swallowed the error,
and the live map rendered as one line while `node --check` and every grep stayed green. This
test executes the real module in node against a fixture-built snapshot (the DOM-stub harness in
``tests/web_harness/ospf_headless.mjs``) and asserts the whole graph drew.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.core.serialize import ospf_topology_to_dict
from obi.web.server import STATIC_DIR
from tests.ospf_fixtures import build_from_fleet

HARNESS = Path(__file__).parent.parent / "web_harness" / "ospf_headless.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _render_summary(tmp_path: Path) -> dict:
    topology = build_from_fleet()
    snapshot = {
        "tool": "ospf",
        "generated_at": "2026-07-12T00:00:00+00:00",
        "data": ospf_topology_to_dict(topology),
    }
    snapshot_path = tmp_path / "ospf.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(HARNESS), str(STATIC_DIR / "widgets" / "ospf.js"), str(snapshot_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_widget_draws_the_whole_fixture_fleet(tmp_path: Path) -> None:
    summary = _render_summary(tmp_path)
    assert summary["mountError"] is None
    # all 12 OSPF routers drew as node circles — a draw() abort leaves this short (the
    # one-line-map regression rendered exactly 1 stroke, 0 circles, 0 texts).
    assert summary["nodeCircles"] == 12
    assert summary["curves"] >= 13  # every formed adjacency (plus peer-resolved ghosts) curved
    assert summary["strokes"] >= summary["curves"]
    assert summary["texts"] > 12  # per-side cost labels + node labels actually rendered
