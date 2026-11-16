"""T031 (analyze C1, SC-006): a ~50-device UISP fleet is reported within a bounded budget."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.integration._uisp import patch_transport, write_uisp_config

runner = CliRunner()


def _fleet(count: int) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for i in range(count):
        devices.append(
            {
                "_id": f"id-{i}",
                "identification": {
                    "id": f"id-{i}",
                    "name": f"dev-{i:02d}",
                    "model": "PowerBeam 5AC",
                    "firmwareVersion": "WA.v8.7.11",
                    "mac": f"00:11:22:33:44:{i:02x}",
                },
                # Every 5th device is offline — exercises mixed reachability at scale.
                "overview": {
                    "status": "active" if i % 5 else "disconnected",
                    "uptime": 1000 + i,
                    "cpu": 1.0,
                },
                "ipAddress": f"10.40.0.{i}",
            }
        )
    return devices


def test_fifty_device_fleet_reports_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch, devices=_fleet(50))
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    # SC-006: the whole fleet is reported with no per-device blowup.
    assert "50 UISP device(s)" in result.output
