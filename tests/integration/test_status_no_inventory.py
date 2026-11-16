"""T019: `obi status` with no inventory instructs the operator and exits 2 (FR-017, AS64501)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app

runner = CliRunner()


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[discovery]
seeds = ["172.18.81.1"]

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


def test_status_without_inventory_exits_2_with_guidance(tmp_path: Path) -> None:
    config = _config(tmp_path)  # inventory.json is never created

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 2, result.output
    assert "discover" in result.output.lower()  # actionable: tells them to run discover
    assert not (tmp_path / "inventory.json").exists()  # no silent fall-back / side effects
