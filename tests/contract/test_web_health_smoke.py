"""Contract smoke (feature 096): the HLT board and the router HEALTH tab actually render.

Drives the REAL widget modules in node against the stub DOM (health_headless.mjs): the
board mounts, keeps the server's worst-first order, names breaches, and drills down on a
row click; the HEALTH tab renders readings with breach marking and polls ONLY while
active. Payload shapes mirror what tests/contract/test_web_health_api.py pins server-side.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_HARNESS = Path(__file__).parent.parent / "web_harness" / "health_headless.mjs"
_STATIC = Path(__file__).parent.parent.parent / "src" / "obi" / "web" / "static"

_FLEET_PAYLOAD = {
    "store": "ok",
    "generated_at": "2026-08-25T12:00:00+00:00",
    "note": None,
    "devices": [
        {
            "device": "hot-gw", "address": "172.18.93.1", "kind": "routeros",
            "worst": "down", "stale": False, "age_seconds": 60,
            "summary": {"temperature_c": 81.0, "state_alerts": ["psu2-state"]},
            "breaches": [
                {"metric": "health:board-temperature2", "instance": "7102",
                 "label": "board-temperature2", "class": "temperature",
                 "value": 81.0, "unit": "°C", "limit": "> 75 °C"},
            ],
        },
        {
            "device": "core-vms-1", "address": "core-vms-1", "kind": "linux",
            "worst": "ok", "stale": False, "age_seconds": 30,
            "summary": {"cpu_percent": 0.8, "memory_percent": 7.5,
                        "disk_max_percent": 42.3},
            "breaches": [],
        },
    ],
}

_DEVICE_PAYLOAD = {
    "store": "ok",
    "device": "chestnut-gw",
    "kind": "routeros",
    "stale": False,
    "age_seconds": 60,
    "note": None,
    "readings": [
        {"metric": "health:cpu-temperature", "instance": "17", "label": "cpu-temperature",
         "value": 35.0, "unit": "°C"},
        {"metric": "optics:rxPowerMdbm", "instance": "2", "label": "sfp-sfpplus1",
         "value": -1.907, "unit": "dBm"},
    ],
    "breaches": [
        {"metric": "health:cpu-temperature", "instance": "17", "label": "cpu-temperature",
         "class": "temperature", "value": 35.0, "unit": "°C", "limit": "> 30 °C"},
    ],
    "series": [
        {"metric": "health:cpu-temperature", "ifname": "cpu-temperature", "unit": "°C",
         "buckets": [{"start": "t0", "avg": 34.0, "min": 34.0, "max": 34.0, "n": 1},
                     {"start": "t1", "avg": 35.0, "min": 35.0, "max": 35.0, "n": 1}]},
    ],
}


def _run(target: str, payload: dict, tmp_path: Path) -> dict:
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload))
    proc = subprocess.run(
        ["node", str(_HARNESS), target, str(_STATIC), str(payload_file)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_hlt_board_renders_worst_first_and_drills_down(tmp_path: Path) -> None:
    result = _run("hlt", _FLEET_PAYLOAD, tmp_path)
    assert result["mountError"] is None
    assert result["rowCount"] == 2
    assert "hot-gw" in result["rows"][0]  # server order preserved (worst first)
    assert "board-temperature2" in result["rows"][0]  # the breach is named
    assert "core-vms-1" in result["rows"][1]
    assert "—" in result["rows"][1]  # absent classes stay dashes, never zeros
    assert result["opened"] == [{"address": "172.18.93.1", "identity": "hot-gw"}]


def test_hlt_board_empty_store_names_the_cli_fix(tmp_path: Path) -> None:
    payload = {"store": "absent", "devices": [],
               "hint": "no health readings yet — run: obi metrics ingest (RouterOS) and "
                       "obi health probe (collector VMs)"}
    result = _run("hlt", payload, tmp_path)
    assert result["mountError"] is None
    assert result["rowCount"] == 0
    assert result["emptyState"] and "obi metrics ingest" in result["emptyState"]


def test_health_tab_renders_readings_and_polls_only_active(tmp_path: Path) -> None:
    result = _run("rhealth", _DEVICE_PAYLOAD, tmp_path)
    assert result["mountError"] is None
    joined = " ".join(result["rows"])
    assert "health:cpu-temperature" in joined
    assert "sfp-sfpplus1" in joined
    assert "▁" in joined or "█" in joined  # the trend sparkline rendered
    assert "1 breaching" in (result["status"] or "")
    polling = result["polling"]
    assert polling["beforeActivate"] == 0  # mount alone starts no interval (D8)
    assert polling["afterActivate"] == 1  # activate starts exactly one
    assert polling["cleared"] == 1  # deactivate stops it
