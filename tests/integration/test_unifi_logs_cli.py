"""T015: `obi unifi logs ingest` → `obi logs query` / `obi unifi-logs status` (feature 016)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers import unifi_internal
from tests.unifi_fakes import FakeUnifiInternalClient, unifi_internal_settings

runner = CliRunner()


def _write_config(tmp_path: Path) -> Path:
    db = tmp_path / "logs.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""
[unifi]
base_url = "https://c.example.com"
sites = ["Alder"]

[unifi-internal]
username = "admin"
password = "pw"

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
def _fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeUnifiInternalClient:
    fake = FakeUnifiInternalClient(unifi_internal_settings(sites=["Alder"]), ignore_window=True)
    # The engine imports UnifiInternalClient lazily from this module at call time.
    monkeypatch.setattr(unifi_internal, "UnifiInternalClient", lambda settings: fake)
    return fake


def test_ingest_then_query_and_status(
    tmp_path: Path, _fake_client: FakeUnifiInternalClient
) -> None:
    config = _write_config(tmp_path)

    ingest = runner.invoke(app, ["unifi", "logs", "ingest", "--scope", str(config)])
    assert ingest.exit_code == 0, ingest.output
    assert "2 new" in ingest.output

    # The events are now queryable through the SAME log surface as RouterOS syslog (US1).
    query = runner.invoke(
        app, ["logs", "query", "--scope", str(config), "--host", "Alder", "--json"]
    )
    assert query.exit_code == 0, query.output
    assert "Trailer" in query.output  # device name rendered into the message
    assert "unifi" in query.output  # tagged as UniFi-sourced (collectors)

    status = runner.invoke(app, ["unifi", "logs", "status", "--scope", str(config), "--json"])
    assert status.exit_code == 0, status.output
    sites = json.loads(status.output)
    assert any(s["site"] == "Alder" for s in sites)


def test_ingest_without_internal_config_errors(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[logs]\ndb_path = "%s"\n' % (tmp_path / "logs.db"))
    cfg.chmod(0o600)
    result = runner.invoke(app, ["unifi", "logs", "ingest", "--scope", str(cfg)])
    assert result.exit_code == 2
    assert "unifi-internal" in result.output
