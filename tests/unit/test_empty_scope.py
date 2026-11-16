"""T026: empty discovery → clear message and a clean exit code 0 (FR-009)."""

from __future__ import annotations

from pathlib import Path

import librouteros
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.audit import AuditTrail
from obi.core.models import DiscoveryScope
from tests.fakes import FakeDriver
from tests.inventory_helpers import run_inventory

runner = CliRunner()


def test_empty_discovery_returns_empty_report(tmp_path: Path) -> None:
    # Nothing configured in the fake → every range candidate probes UNREACHABLE and is dropped.
    driver = FakeDriver({})
    scope = DiscoveryScope(ranges=["10.99.99.0/30"])  # hosts .1 and .2

    report = run_inventory(
        scope, driver, audit=AuditTrail(tmp_path / "audit.jsonl"), timeout=0.5, concurrency=8
    )

    assert report.statuses == []
    assert report.counts["total"] == 0


def test_cli_discover_all_seeds_unreachable_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # All seeds refuse the connection — `obi routeros discover` still writes a clean inventory (the
    # seeds recorded unreachable, never dropped) and exits 0 (feature 002, US4 AS64504).
    def refuse(**kwargs: object) -> None:
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(librouteros, "connect", refuse)

    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[discovery]
seeds = ["10.99.99.1"]

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

    result = runner.invoke(app, ["routeros", "discover", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "10.99.99.1" in result.output  # the seed is recorded, not dropped
    assert "unreachable" in result.output.lower()
