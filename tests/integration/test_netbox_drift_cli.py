"""T009: `obi netbox drift` CLI (feature 017) — wiring, JSON, and resilience.

The classification matrix is exhaustively unit-tested in tests/unit/test_reconcile.py; this asserts
the command renders, emits valid JSON, degrades cleanly when NetBox is down, and never crashes on an
absent roster. The real NetboxSource + mapping run via the shared `patch_transport` helper (007).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core import inventory as inv
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import DiscoveryScope, DiscoveryState
from tests.integration._netbox import patch_transport, write_netbox_config

runner = CliRunner()
SCOPE = DiscoveryScope(ranges=["172.18.0.0/16"], seeds=["172.18.89.1"])


def _raw(
    name: str, ip: str | None, *, manufacturer: str = "MikroTik", model: str = "CCR2004"
) -> dict[str, Any]:
    """A raw NetBox /api/dcim/devices/ element (the shape the 007 adapter maps)."""

    return {
        "name": name,
        "site": {"name": "Alder"},
        "role": {"name": "Router"},
        "device_type": {"model": model, "manufacturer": {"name": manufacturer}},
        "status": {"value": "active"},
        "primary_ip": {"address": ip} if ip else None,
        "serial": None,
    }


def _write_roster(path: Path) -> None:
    entries = [
        InventoryEntry(
            address="172.18.86.1", identity="walnut-gw", discovery_state=DiscoveryState.IDENTIFIED
        ),
        InventoryEntry(
            address="172.18.74.1",
            identity="chestnut-internet-gw",
            discovery_state=DiscoveryState.IDENTIFIED,
        ),
    ]
    inv.save(Inventory(scope=SCOPE, entries=entries), path)


def test_drift_table_renders_grouped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_netbox_config(tmp_path)
    _write_roster(tmp_path / "inventory.json")
    patch_transport(
        monkeypatch,
        devices=[
            _raw("walnut-gw", "172.18.86.1/24"),
            _raw("core-gw-2", "172.18.84.12/24"),
            _raw("a-switch", "172.18.84.99/24", manufacturer="Ubiquiti"),  # non-MikroTik
        ],
    )
    result = runner.invoke(app, ["netbox", "drift", "--scope", str(config)])
    assert result.exit_code == 0, result.output
    assert "ADD" in result.output and "chestnut-internet-gw" in result.output
    assert "RETIRE CANDIDATES" in result.output and "core-gw-2" in result.output
    assert "a-switch" not in result.output  # non-MikroTik never appears as RouterOS drift


def test_drift_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_netbox_config(tmp_path)
    _write_roster(tmp_path / "inventory.json")
    patch_transport(monkeypatch, devices=[_raw("walnut-gw", "172.18.86.1/24")])
    result = runner.invoke(app, ["netbox", "drift", "--scope", str(config), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["counts"]["matched"] == 1
    assert payload["counts"]["undocumented"] == 1  # chestnut-internet-gw
    assert {d["name"] for d in payload["drift"]} == {"walnut-gw", "chestnut-internet-gw"}


def test_drift_netbox_unreachable_exits_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_netbox_config(tmp_path)
    _write_roster(tmp_path / "inventory.json")
    patch_transport(monkeypatch, error=httpx.ConnectError("nope"))
    result = runner.invoke(app, ["netbox", "drift", "--scope", str(config)])
    assert result.exit_code == 0, result.output  # degrades, never crashes (FR-011)
    assert "unreachable" in result.output.lower()


def test_drift_absent_roster_exits_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_netbox_config(tmp_path)  # no roster written
    patch_transport(monkeypatch, devices=[_raw("walnut-gw", "172.18.86.1/24")])
    result = runner.invoke(app, ["netbox", "drift", "--scope", str(config)])
    assert result.exit_code == 0, result.output
    assert "no live reality" in result.output.lower()
