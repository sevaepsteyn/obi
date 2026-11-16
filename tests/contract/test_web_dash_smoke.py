"""Contract: the DASH board actually RENDERS (feature 095) — pure view rules + a real mount.

Executes the REAL widget in node (tests/web_harness/dash_headless.mjs) against a stubbed
/api/dash.json: the pure staleness/sort/badge rules with fixed clocks, and a mount smoke over
the ok / absent / stale payload shapes. Synthetic fixtures only; skipped where node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from obi.web.server import STATIC_DIR

HARNESS = Path(__file__).parent.parent / "web_harness" / "dash_headless.mjs"
DASH_JS = STATIC_DIR / "widgets" / "dash.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(tmp_path: Path, payload: dict) -> dict:
    fixture = tmp_path / "payload.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(HARNESS), str(DASH_JS), str(fixture)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _board(*, age_s: float = 30.0) -> dict:
    generated_us = round((time.time() - age_s) * 1_000_000)
    return {
        "generated_at": "2026-08-25T00:00:00+00:00",
        "generated_at_us": generated_us,
        "state": "red",
        "notes": ["uisp snapshot absent"],
        "tiles": [
            {
                "signal": signal,
                "state": "red" if signal == "fleet" else "green",
                "cadence_s": 60,
                "data_age_s": 4.0,
                "items": [
                    {"key": "x", "label": "x", "state": "red" if signal == "fleet" else "green",
                     "detail": "down 14m", "metrics": {}, "since_us": None}
                ],
            }
            for signal in ("fleet", "netwatch", "ping", "internet", "pipeline")
        ],
    }


def test_pure_view_rules(tmp_path: Path) -> None:
    out = _run(tmp_path, {"store": "ok", "hint": None, "board": _board()})
    pure = out["pure"]
    assert pure["freshIsStale"] is False  # 1 minute old — live
    assert pure["oldIsStale"] is True  # 10 minutes old — past the 3-minute honesty line
    assert pure["isoFallbackIsStale"] is False  # ISO-only boards still judge (shots fixture path)
    assert pure["sortedKeys"] == ["c", "e", "d", "a", "b"]  # red, yellow, stale, then greens A→Z
    assert pure["badgeFresh"].startswith("yellow · ")
    assert pure["badgeStale"].startswith("stale (")  # a stale board never advertises its old state
    assert pure["badgeDegraded"].endswith("refresh failing")
    assert pure["tileStateForced"] == "stale"  # a stale board forces every tile stale


def test_mount_renders_five_tiles_in_order(tmp_path: Path) -> None:
    out = _run(tmp_path, {"store": "ok", "hint": None, "board": _board()})
    assert out["mountError"] is None, out["mountError"]
    assert out["tiles"] == ["dash-red", "dash-green", "dash-green", "dash-green", "dash-green"]
    assert out["banner"] is False
    assert out["noteCount"] == 1
    assert out["badge"].startswith("red · ")


def test_mount_stale_board_forces_stale_everywhere(tmp_path: Path) -> None:
    out = _run(tmp_path, {"store": "ok", "hint": None, "board": _board(age_s=600)})
    assert out["mountError"] is None, out["mountError"]
    assert out["tiles"] == ["dash-stale"] * 5  # persisted red must not read as live judgment
    assert out["banner"] is True
    assert out["badge"].startswith("stale (")


def test_mount_absent_store_shows_the_fixing_command(tmp_path: Path) -> None:
    hint = "no dashboard snapshot yet — run: obi dash run (cron)"
    out = _run(tmp_path, {"store": "absent", "hint": hint})
    assert out["mountError"] is None, out["mountError"]
    assert out["tiles"] == []
    assert out["message"] == "no dashboard snapshot yet — run: obi dash run (cron)"
    assert out["badge"] is None


def test_maintenance_pure_rules(tmp_path: Path) -> None:
    # 135: the browser twin of the CLI/dash_status partition and banner.
    pure = _run(tmp_path, {"store": "ok", "hint": None, "board": _board()})["pure"]
    assert pure["partition"] == {"problems": ["r"], "maint": ["m"], "ok": ["g"]}
    assert pure["maintLine"] == (
        "sites magnolia, manzanita; signals mesh; 21 UISP-flagged radios — teardown"
        " [matches nothing: site shorline]"
    )
    assert pure["maintLineNone"] is None
    assert pure["tileStateMaint"] == "maint"
    assert pure["tileStateMaintStale"] == "stale"  # the honesty rule still wins


def test_mount_renders_maintenance_banner_and_group(tmp_path: Path) -> None:
    board = _board()
    board["tiles"][0]["items"].append(
        {
            "key": "magnolia-gw",
            "label": "magnolia-gw",
            "state": "yellow",
            "detail": "maintenance · down",
            "metrics": {"maintenance": 1},
            "since_us": None,
            "maintenance": "config: teardown",
        }
    )
    board["tiles"].append(
        {"signal": "mesh", "state": "yellow", "cadence_s": 300, "data_age_s": None,
         "maintenance": "config",
         "items": [{"key": "radio", "label": "mesh radio", "state": "yellow",
                    "detail": "maintenance", "metrics": {}, "since_us": None,
                    "maintenance": "config"}]}
    )
    board["maintenance"] = {"sites": ["magnolia"], "signals": ["mesh"], "reason": "teardown",
                            "uisp_flagged": 0, "unmatched": [], "items": 2}
    out = _run(tmp_path, {"store": "ok", "hint": None, "board": board})
    assert out["mountError"] is None, out["mountError"]
    assert out["maintBanner"] == "MAINT: sites magnolia; signals mesh — teardown"
    assert out["tiles"][-1] == "dash-maint"
    assert "maintenance (1): magnolia-gw" in out["maintGroups"]
    assert out["counts"][0] == "0/1"  # the maintenance item counts as neither ok nor total
