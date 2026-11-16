"""T022: the UISP API token never appears in output, the audit trail, or logs (FR-009, SC-004)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers.uisp import UispError
from obi.core.models import SourceOutcome
from tests.integration._uisp import patch_transport, write_uisp_config

runner = CliRunner()

_TOKEN = "UISP-TOKEN-supersecret"  # the value written by write_uisp_config()


def test_token_absent_from_output_and_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    assert _TOKEN not in result.output
    audit = (tmp_path / "audit.jsonl").read_text()
    assert _TOKEN not in audit
    assert '"uisp_status"' in audit  # the query WAS audited (FR-010), just without the token


def test_token_absent_even_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(
        monkeypatch, error=UispError(SourceOutcome.AUTH_FAILED, "controller rejected the API token")
    )
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert _TOKEN not in result.output
    assert _TOKEN not in (tmp_path / "audit.jsonl").read_text()
