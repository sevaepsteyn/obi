"""T017: the `--filter` option focuses the UISP report on matching device names (FR-016)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.integration._uisp import patch_transport, write_uisp_config

runner = CliRunner()


def test_filter_limits_to_matching_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config), "--filter", "ptp"])

    assert result.exit_code == 0, result.output
    assert "ptp-ridge-east" in result.output
    assert "ptp-ridge-west" in result.output
    assert "core-sw-01" not in result.output  # filtered out (SC-003)


def test_filter_from_config_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path, extra='name_filter = "ptp"')

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "core-sw-01" not in result.output
    assert "ptp-ridge-east" in result.output


def test_filter_no_match_is_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config), "--filter", "zzz-nomatch"])

    assert result.exit_code == 0, result.output  # empty, not an error
    assert "No UISP devices matched" in result.output
