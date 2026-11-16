"""T022: end-to-end table output via the CLI — now inventory-sourced (feature 002).

`obi status` reads the persisted inventory, so each test first runs `obi routeros discover` to build
it (librouteros stubbed by a fixture), then runs `obi status`.
"""

from __future__ import annotations

from pathlib import Path

import librouteros
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.fakes import RecordingConnection, load_fixture

runner = CliRunner()


def _write_config(tmp_path: Path, seeds: list[str]) -> Path:
    seed_list = ", ".join(f'"{s}"' for s in seeds)
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[discovery]
seeds = [{seed_list}]
ranges = ["172.18.81.0/24"]

[routeros]
username = "admin"
password = "fakesecret44028f"
use_tls = true

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

    def fake_connect(**kwargs: object) -> RecordingConnection:
        # Per-host serial so distinct addresses stay distinct devices under collapse (feature 018).
        routerboard = dict(fixture["routerboard"])
        routerboard["serial-number"] = f"{base_serial}-{kwargs.get('host', '?')}"
        records = {
            ("system", "identity"): [fixture["identity"]],
            ("system", "resource"): [fixture["resource"]],
            ("system", "routerboard"): [routerboard],
            ("ip", "neighbor"): fixture["neighbors"],
        }
        return RecordingConnection(records)

    monkeypatch.setattr(librouteros, "connect", fake_connect)
    monkeypatch.setenv("COLUMNS", "240")


def test_status_table_lists_device_and_fields(tmp_path: Path, fixture_connect: None) -> None:
    config = _write_config(tmp_path, ["172.18.81.1"])

    discover = runner.invoke(app, ["routeros", "discover", "--scope", str(config)])
    assert discover.exit_code == 0, discover.output

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    output = result.output
    assert "border-gw-01" in output  # SC-005: identity + health from the inventory
    assert "172.18.81.1" in output
    assert "RB750Gr3" in output  # model
    assert "7.11.2" in output  # RouterOS version
    assert "reachable" in output
    assert "device(s)" in output


def test_status_no_inventory_directs_to_discover(tmp_path: Path) -> None:
    config = _write_config(tmp_path, ["172.18.81.1"])  # no discover run → no inventory file

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 2, result.output
    assert "discover" in result.output.lower()


def test_status_writes_audit_record(tmp_path: Path, fixture_connect: None) -> None:
    config = _write_config(tmp_path, ["172.18.81.1"])
    runner.invoke(app, ["routeros", "discover", "--scope", str(config)])

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert any('"routeros_status"' in line for line in audit_lines)
    # Credentials never reach the audit trail.
    assert all("fakesecret44028f" not in line for line in audit_lines)
