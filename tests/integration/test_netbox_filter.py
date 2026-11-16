"""T017: the case-insensitive device-name filter for `obi netbox inventory` (US2, FR-016)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.integration._netbox import patch_transport, write_netbox_config

runner = CliRunner()


def _names(output: str) -> set[str]:
    return {d["name"] for d in json.loads(output)["devices"]}


def test_filter_matches_subset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(
        app, ["netbox", "inventory", "--scope", str(config), "--filter", "chestnut", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert _names(result.output) == {"chestnut-gw", "chestnut-ptp-zelkova-60"}


def test_filter_is_case_insensitive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(
        app, ["netbox", "inventory", "--scope", str(config), "--filter", "CORE", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert _names(result.output) == {"core-gw"}


def test_unset_filter_returns_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    assert len(_names(result.output)) == 6  # includes the unnamed device (None)


def test_no_match_is_clean_empty_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config), "--filter", "zzz"])

    assert result.exit_code == 0, result.output  # not an error (FR-016)
    assert "No NetBox devices matched the filter." in result.output


def test_config_name_filter_is_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_netbox_config(tmp_path, extra='name_filter = "chestnut"')

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    assert _names(result.output) == {"chestnut-gw", "chestnut-ptp-zelkova-60"}
