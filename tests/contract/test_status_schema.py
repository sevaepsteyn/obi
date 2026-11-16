"""T033 (analyze G1): `obi status --json` conforms to the 002 status-output schema."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import librouteros
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.fakes import RecordingConnection, load_fixture

runner = CliRunner()

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "tests" / "schemas" / "discover-status-output.schema.json"
    ).read_text()
)


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[discovery]
seeds = ["172.18.81.1"]
ranges = ["172.18.81.0/24"]

[routeros]
username = "admin"
password = "fakesecret44028f"

[audit]
path = "{tmp_path / 'audit.jsonl'}"

[inventory]
path = "{tmp_path / 'inventory.json'}"
"""
    )
    config.chmod(0o600)
    return config


@pytest.fixture
def fixture_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = load_fixture("border-gw-01")
    base_serial = str(fixture["routerboard"].get("serial-number") or "SN")

    def _connect(**kwargs: object) -> RecordingConnection:
        # Distinct addresses are distinct boxes ⇒ distinct serials, so serial-based collapse
        # (feature 018) does not fold the seed and its neighbor into one.
        routerboard = dict(fixture["routerboard"])
        routerboard["serial-number"] = f"{base_serial}-{kwargs.get('host', '?')}"
        records = {
            ("system", "identity"): [fixture["identity"]],
            ("system", "resource"): [fixture["resource"]],
            ("system", "routerboard"): [routerboard],
            ("ip", "neighbor"): fixture["neighbors"],
        }
        return RecordingConnection(records)

    monkeypatch.setattr(librouteros, "connect", _connect)


def test_status_json_conforms_to_002_schema(tmp_path: Path, fixture_connect: None) -> None:
    config = _config(tmp_path)
    assert runner.invoke(app, ["routeros", "discover", "--scope", str(config)]).exit_code == 0

    result = runner.invoke(app, ["status", "--json", "--scope", str(config)])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    jsonschema.validate(instance=payload, schema=_SCHEMA)

    by_addr = {d["address"]: d for d in payload["devices"]}
    # The crawled seed carries the new discovery fields + live health.
    seed = by_addr["172.18.81.1"]
    assert seed["discovery_state"] == "identified"
    assert seed["provenance"] == "seed"
    assert seed["present_in_latest"] is True
    assert seed["reachability"] == "reachable"
    # Mikrotik-only roster: the non-Mikrotik (Ubiquiti) neighbor is skipped, not in the report.
    assert "172.18.81.50" not in by_addr
