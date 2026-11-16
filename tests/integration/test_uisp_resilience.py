"""T021: a slow/unreachable/auth-failing controller yields a clean, guided result.

See FR-011 and SC-005.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers.uisp import UispError
from obi.core.models import SourceOutcome
from tests.integration._uisp import patch_transport, write_uisp_config

runner = CliRunner()


@pytest.mark.parametrize(
    "error, marker",
    [
        (UispError(SourceOutcome.UNREACHABLE, "could not connect to controller"), "unreachable"),
        (UispError(SourceOutcome.AUTH_FAILED, "controller rejected the API token"), "auth_failed"),
        (UispError(SourceOutcome.TIMED_OUT, "controller did not respond"), "timed_out"),
    ],
)
def test_controller_failure_is_clean_and_guided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: UispError, marker: str
) -> None:
    patch_transport(monkeypatch, error=error)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output  # exits without error (SC-005)
    assert "Traceback" not in result.output  # no crash / stacktrace
    assert marker in result.output  # the controller-level outcome is surfaced


def test_unexpected_transport_error_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch, error=RuntimeError("kaboom"))
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "unreachable" in result.output
