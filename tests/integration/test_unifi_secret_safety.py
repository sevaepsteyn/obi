"""T028: UniFi credentials never appear in output, the audit trail, or logs (FR-009, SC-005)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers.unifi_common import UnifiError
from obi.core.models import SourceOutcome
from tests.integration._unifi import patch_transports, write_unifi_config

runner = CliRunner()

_KEY = "UNIFI-KEY-supersecret"  # written by write_unifi_config()
_TOKEN = "UNIFI-TOKEN-supersecret"


def test_credentials_absent_from_output_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transports(monkeypatch)
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    assert _KEY not in result.output and _TOKEN not in result.output
    audit = (tmp_path / "audit.jsonl").read_text()
    assert _KEY not in audit and _TOKEN not in audit
    assert '"unifi_status"' in audit  # audited (FR-010), without the credentials


def test_credentials_absent_even_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transports(
        monkeypatch,
        official_error=UnifiError(SourceOutcome.AUTH_FAILED, "integration API rejected"),
        controller_error=UnifiError(SourceOutcome.AUTH_FAILED, "controller API rejected"),
    )
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert _KEY not in result.output and _TOKEN not in result.output
    assert _KEY not in (tmp_path / "audit.jsonl").read_text()
    assert _TOKEN not in (tmp_path / "audit.jsonl").read_text()
