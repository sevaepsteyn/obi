"""T013: `obi uisp logs ingest` → `obi logs query` / `obi uisp logs status` (feature 019)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers import uisp as uisp_mod
from tests.fakes import uisp_log_transport

runner = CliRunner()


def _write_config(tmp_path: Path) -> Path:
    db = tmp_path / "logs.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""
[uisp]
base_url = "https://uisp.example.com"
token = "UISP-TOKEN-supersecret"
ingest_events = true
ingest_outages = true

[logs]
db_path = "{db}"
log_tz = "UTC"

[audit]
path = "{tmp_path / "audit.jsonl"}"
"""
    )
    cfg.chmod(0o600)
    return cfg


@pytest.fixture
def _fake_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    real = uisp_mod.UispController
    # The engine imports UispController lazily from this module at call time.
    monkeypatch.setattr(
        uisp_mod,
        "UispController",
        lambda settings: real(settings, log_transport=uisp_log_transport()),
    )


def test_ingest_then_query_and_status(tmp_path: Path, _fake_controller: None) -> None:
    config = _write_config(tmp_path)

    ingest = runner.invoke(app, ["uisp", "logs", "ingest", "--scope", str(config)])
    assert ingest.exit_code == 0, ingest.output
    assert "new" in ingest.output
    assert "logs:" in ingest.output and "outages:" in ingest.output

    # The records are now queryable through the SAME log surface as RouterOS syslog (US1).
    query = runner.invoke(
        app, ["logs", "query", "--scope", str(config), "--source", "uisp", "--json"]
    )
    assert query.exit_code == 0, query.output
    assert "ptp-ridge-east" in query.output  # device name rendered into the message
    assert "uisp" in query.output  # tagged as UISP-sourced (collectors)

    status = runner.invoke(app, ["uisp", "logs", "status", "--scope", str(config), "--json"])
    assert status.exit_code == 0, status.output
    sources = {s["source"] for s in json.loads(status.output)}
    assert {"logs", "outages"} <= sources


def test_ingest_without_ingestion_config_errors(tmp_path: Path) -> None:
    db = tmp_path / "logs.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[uisp]\nbase_url = "https://u.example.com"\ntoken = "t"\n[logs]\ndb_path = "{db}"\n'
    )
    cfg.chmod(0o600)
    result = runner.invoke(app, ["uisp", "logs", "ingest", "--scope", str(cfg)])
    assert result.exit_code == 2
    assert "ingest_events" in result.output or "ingestion not configured" in result.output
