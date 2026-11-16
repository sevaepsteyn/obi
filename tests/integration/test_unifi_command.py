"""The dedicated `obi unifi status` and `obi unifi sm` commands (feature 011)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers import unifi_sm as sm_mod
from obi.core.drivers.unifi_sm import _flatten_sitemanager
from tests.fakes import load_unifi_sitemanager
from tests.integration._unifi import patch_transports, write_unifi_config

runner = CliRunner()


# --- obi unifi status -------------------------------------------------------------------------


def test_network_command_lists_devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transports(monkeypatch)
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["unifi", "status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "udm-ridge" in result.output
    assert "unifi" in result.output  # vendor label
    assert "UniFi device(s)" in result.output


def test_network_command_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transports(monkeypatch)
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["unifi", "status", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["console_outcome"] == "reachable"
    assert len(payload["devices"]) == 4
    assert '"unifi_status"' in (tmp_path / "audit.jsonl").read_text()


def test_network_command_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transports(monkeypatch)
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["unifi", "status", "--scope", str(config), "--filter", "ridge"])

    assert result.exit_code == 0, result.output
    assert "udm-ridge" in result.output
    assert "usw-core" not in result.output


def test_network_command_errors_when_not_configured(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[uisp]\nbase_url = "https://u"\ntoken = "t"\n')
    config.chmod(0o600)

    result = runner.invoke(app, ["unifi", "status", "--scope", str(config)])

    assert result.exit_code == 2
    assert "unifi not configured" in result.output


# --- obi unifi sm ------------------------------------------------------------------------------


def _write_sm_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        "[unifi-sm]\n"
        'api_key = "UNIFI-SM-KEY-supersecret"\n'
        "[audit]\n"
        f'path = "{tmp_path / "audit.jsonl"}"\n'
        "[inventory]\n"
        f'path = "{tmp_path / "inventory.json"}"\n'
    )
    config.chmod(0o600)
    return config


def test_sm_command_lists_devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flat = _flatten_sitemanager(load_unifi_sitemanager())
    monkeypatch.setattr(
        sm_mod.UnifiSiteManagerController, "_default_transport", lambda self, t: list(flat)
    )
    monkeypatch.setenv("COLUMNS", "240")
    config = _write_sm_config(tmp_path)

    result = runner.invoke(app, ["unifi", "sm", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["console_outcome"] == "reachable"
    assert {d["identity"] for d in payload["devices"]} == {"sm-gw-01", "sm-ap-01", "sm-sw-01"}
    audit = (tmp_path / "audit.jsonl").read_text()
    assert '"unifi_sitemanager"' in audit
    assert "UNIFI-SM-KEY-supersecret" not in audit  # the key is never audited


def test_sm_command_errors_when_not_configured(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[uisp]\nbase_url = "https://u"\ntoken = "t"\n')
    config.chmod(0o600)

    result = runner.invoke(app, ["unifi", "sm", "--scope", str(config)])

    assert result.exit_code == 2
    assert "unifi-sm not configured" in result.output
