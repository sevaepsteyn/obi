"""T017: a degraded console (one surface down) still yields the full official list (FR-007)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers.unifi_common import UnifiError
from obi.core.models import SourceOutcome
from tests.integration._unifi import patch_transports, write_unifi_config

runner = CliRunner()


def test_controller_down_still_lists_official_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transports(
        monkeypatch,
        controller_error=UnifiError(SourceOutcome.UNREACHABLE, "controller API connection refused"),
    )
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output  # partial result, clean exit
    out = result.output
    assert "udm-ridge" in out  # official inventory still shown
    assert "uap-attic" not in out  # controller-only device gone (its surface was down)
    assert "partial result" in out  # the gap is surfaced to the operator (FR-007)
    assert "controller API connection refused" in out


def test_official_down_falls_back_to_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transports(
        monkeypatch,
        official_error=UnifiError(SourceOutcome.TIMED_OUT, "integration API timed out"),
    )
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "udm-ridge" in out  # still present via the controller surface
    assert "uap-attic" in out  # controller-only device present
    assert "partial result" in out
