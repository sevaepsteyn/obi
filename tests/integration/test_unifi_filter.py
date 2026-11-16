"""T029: the --filter narrows a UniFi status query; a no-match filter is a clean empty (FR-011)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.integration._unifi import patch_transports, write_unifi_config

runner = CliRunner()


def test_filter_narrows_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transports(monkeypatch)
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config), "--filter", "ridge"])

    assert result.exit_code == 0, result.output
    assert "udm-ridge" in result.output
    assert "usw-core" not in result.output
    assert "uap-attic" not in result.output


def test_filter_matching_nothing_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transports(monkeypatch)
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config), "--filter", "does-not-exist"])

    assert result.exit_code == 0, result.output  # empty match is success, not error
    assert "No UniFi devices matched the filter." in result.output


def test_config_default_name_filter_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transports(monkeypatch)
    config = write_unifi_config(tmp_path, extra='name_filter = "core"')

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "usw-core" in result.output
    assert "udm-ridge" not in result.output
