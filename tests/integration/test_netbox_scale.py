"""T027: SC-006 — a large documented inventory (multi-page scale) is reported, bounded."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.integration._netbox import patch_transport, write_netbox_config

runner = CliRunner()


def _many_devices(n: int) -> list[dict[str, object]]:
    return [
        {
            "name": f"device-{i:03d}",
            "site": {"name": f"site-{i % 11}"},
            "role": {"name": "switch"},
            "device_type": {"model": "CRS354", "manufacturer": {"name": "MikroTik"}},
            "status": {"value": "active", "label": "Active"},
            "primary_ip": {"address": f"172.18.{i % 256}.1/24"},
            "serial": f"SN{i:05d}",
        }
        for i in range(n)
    ]


def test_large_inventory_reported_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch, devices=_many_devices(200))
    config = write_netbox_config(tmp_path)

    started = time.monotonic()
    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config), "--json"])
    elapsed = time.monotonic() - started

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["counts"]["total"] == 200  # SC-002: none dropped
    assert payload["counts"]["active"] == 200
    assert elapsed < 30.0  # SC-006: bounded, predictable (offline this is near-instant)
