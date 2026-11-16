"""T012: end-to-end `obi netbox inventory` for a NetBox-only config (live, fixture-backed)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.integration._netbox import patch_transport, write_netbox_config

runner = CliRunner()

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "schemas" / "netbox-output.schema.json"
)


def test_netbox_lists_documented_devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "core-gw" in out  # SC-001: documented device listed without manual enumeration
    assert "172.18.84.1/24" in out  # primary IP
    assert "CCR2004-1G-12S+2XS" in out  # model
    assert "MikroTik" in out  # manufacturer
    assert "active" in out and "planned" in out  # documented status passed through (FR-019)
    assert "documented device(s)" in out


def test_netbox_json_conforms_to_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    jsonschema.validate(instance=payload, schema=json.loads(_SCHEMA_PATH.read_text()))
    assert payload["source_outcome"] == "reachable"
    assert payload["source"] == "https://netbox.example.com"
    names = {d["name"] for d in payload["devices"]}
    assert {"core-gw", "chestnut-gw", "esd-ap-01"} <= names
    assert payload["counts"]["total"] == 6


def test_netbox_json_has_no_live_health_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NetBox is documentation, not live health (FR-004, SC-008).
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config), "--json"])

    payload = json.loads(result.output)
    for device in payload["devices"]:
        assert not (
            {"cpu_load_percent", "free_memory_bytes", "uptime_seconds", "reachability"}
            & device.keys()
        )


def test_netbox_writes_audit_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert any('"netbox_inventory"' in line for line in audit_lines)


def test_netbox_does_not_persist_to_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FR-017: `obi netbox inventory` is live; it never writes feature 002's persisted roster.
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "inventory.json").exists()
