"""Vendor-scoped status subcommands: `obi routeros status`, `obi uisp status`.

Each shows exactly its vendor's section of the combined `obi status` report and nothing else
(`obi unifi status` is covered in test_unifi_command.py). On a config missing that vendor's
section, the subcommand exits 2 with an actionable message instead of silently printing nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import librouteros
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.fakes import RecordingConnection, load_fixture
from tests.integration._uisp import patch_transport, write_uisp_config

runner = CliRunner()


@pytest.fixture
def fixture_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = load_fixture("border-gw-01")

    def fake_connect(**kwargs: object) -> RecordingConnection:
        records = {
            ("system", "identity"): [fixture["identity"]],
            ("system", "resource"): [fixture["resource"]],
            ("system", "routerboard"): [fixture["routerboard"]],
            ("ip", "neighbor"): fixture["neighbors"],
        }
        return RecordingConnection(records)

    monkeypatch.setattr(librouteros, "connect", fake_connect)
    monkeypatch.setenv("COLUMNS", "240")


def test_routeros_status_shows_only_the_mikrotik_section(
    tmp_path: Path, fixture_connect: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch)  # UISP controller stubbed too — it must NOT be queried
    config = write_uisp_config(tmp_path, routeros=True)
    assert runner.invoke(app, ["routeros", "discover", "--scope", str(config)]).exit_code == 0

    result = runner.invoke(app, ["routeros", "status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "border-gw-01" in result.output
    assert "reachable" in result.output
    assert "UISP device(s)" not in result.output  # scoped: no UISP section


def test_routeros_status_json(tmp_path: Path, fixture_connect: None) -> None:
    config = write_uisp_config(tmp_path, routeros=True)
    assert runner.invoke(app, ["routeros", "discover", "--scope", str(config)]).exit_code == 0

    result = runner.invoke(app, ["routeros", "status", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)  # exactly ONE document — no UISP JSON appended
    assert any(d["identity"] == "border-gw-01" for d in payload["devices"])


def test_routeros_status_without_inventory_directs_to_discover(
    tmp_path: Path, fixture_connect: None
) -> None:
    config = write_uisp_config(tmp_path, routeros=True)  # no discover run → no inventory

    result = runner.invoke(app, ["routeros", "status", "--scope", str(config)])

    assert result.exit_code == 2
    assert "routeros discover" in result.output


def test_routeros_status_errors_when_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path)  # UISP-only config

    result = runner.invoke(app, ["routeros", "status", "--scope", str(config)])

    assert result.exit_code == 2
    assert "routeros not configured" in result.output


def test_uisp_status_shows_only_the_uisp_section(
    tmp_path: Path, fixture_connect: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path, routeros=True)
    assert runner.invoke(app, ["routeros", "discover", "--scope", str(config)]).exit_code == 0

    result = runner.invoke(app, ["uisp", "status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "UISP device(s)" in result.output
    assert "ptp-ridge-east" in result.output
    assert "border-gw-01" not in result.output  # scoped: no Mikrotik section


def test_uisp_status_filter_and_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(
        app, ["uisp", "status", "--scope", str(config), "--json", "--filter", "ridge-east"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["controller_outcome"] == "reachable"
    assert {d["identity"] for d in payload["devices"]} == {"ptp-ridge-east"}


def test_uisp_status_errors_when_not_configured(
    tmp_path: Path, fixture_connect: None
) -> None:
    config_lines = [
        "[discovery]",
        'seeds = ["172.18.81.1"]',
        "[routeros]",
        'username = "admin"',
        'password = "fakesecret44028f"',
        "use_tls = true",
        "[audit]",
        f'path = "{tmp_path / "audit.jsonl"}"',
        "[inventory]",
        f'path = "{tmp_path / "inventory.json"}"',
    ]
    config = tmp_path / "config.toml"
    config.write_text("\n".join(config_lines) + "\n")
    config.chmod(0o600)

    result = runner.invoke(app, ["uisp", "status", "--scope", str(config)])

    assert result.exit_code == 2
    assert "uisp not configured" in result.output
