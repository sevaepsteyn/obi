"""The demo boot shim intercepts every widget URL (feature 136, T019). Skipped without node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "demo_boot.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    shutil.copytree(STATIC_DIR / "demo", root / "demo")
    (root / "shell.js").write_text(
        "export async function boot() { globalThis.SHELL_LOADED = true; }\n"
    )
    (root / "links.json").write_text('{"generated_at": "2025-06-05T00:00:00+00:00"}')
    data = root / "data"
    for rel, payload in {
        "manifest.json": {"format_version": 1, "demo_now": "2025-06-05T15:59:59-07:00"},
        "snapshots/ospf.json": {"data": {"nodes": []}},
        "snapshots/status.json": {"data": {"devices": []}},
        "logs-slice.json": {"entries": [], "cursor": 9, "of": 0},
        "audit-slice.json": {"events": [], "total": 0, "details": {}, "traces": {},
                             "stats": {"24h": {"by_kind": {}}},
                             "store": {"present": True, "enabled": True}},
        "api/dash/index.json": {"store": "ok", "board": {}},
        "api/inventory/index.json": {"store": "ok", "inventory": {}},
        "api/health/index.json": {"store": "ok", "devices": []},
        "api/metrics/index.json": {"store": "ok", "mode": "coverage", "devices": []},
        "api/configs/index.json": {"store": "ok", "mode": "status", "rows": []},
        "api/watch/index.json": {"store": "absent", "findings": []},
        "api/radio/index.json": {"store": "absent", "entries": []},
    }.items():  # fmt: skip
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        (data / rel).write_text(json.dumps(payload))
    return root


def test_every_widget_url_is_answered(tmp_path: Path) -> None:
    proc = subprocess.run(
        ["node", str(HARNESS), str(_bundle(tmp_path))],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["shellLoaded"] is True and out["demo"] == 1
    assert out["demoNowYear"] == 2025  # the frozen clock is installed before the shell
    assert out["rootRelativeLinks"] is True  # /links.json re-anchored to the bundle root
    for answer in out["answers"]:
        assert answer["json"] or answer["url"] == "/healthz", answer
        assert answer["status"] == 200, answer
    phy = next(a for a in out["answers"] if a["url"].startswith("/api/phy"))
    assert "not available in the demo" in phy["hint"]
