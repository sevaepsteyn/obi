"""T021: the NetBox API token never appears in output, the audit trail, or logs (FR-009, SC-004)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.integration._netbox import patch_transport, write_netbox_config

runner = CliRunner()

# The token written by write_netbox_config (and used by the real Authorization header).
_TOKEN = "NETBOX-TOKEN-supersecret"


def test_token_absent_from_table_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert _TOKEN not in result.output


def test_token_absent_from_json_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    assert _TOKEN not in result.output


def test_token_absent_from_audit_trail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    runner.invoke(app, ["netbox", "inventory", "--scope", str(config)])

    audit = (tmp_path / "audit.jsonl").read_text()
    assert _TOKEN not in audit
