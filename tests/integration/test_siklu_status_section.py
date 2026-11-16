"""Integration: Siklu joins `obi status` as its own section; other vendors unchanged (020)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.integration._siklu import patch_sessions, write_siklu_config
from tests.integration._uisp import patch_transport, write_uisp_config

runner = CliRunner()


def test_status_includes_siklu_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_sessions(monkeypatch)
    config = write_siklu_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "Siklu EtherHaul radios" in result.output
    assert "tamarack-ptp-basswood-70" in result.output
    assert "Siklu radio(s)" in result.output


def test_status_siklu_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_sessions(monkeypatch)
    config = write_siklu_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert {r["vendor"] for r in payload["radios"]} == {"siklu"}


def test_status_uisp_output_unchanged_by_siklu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding [siklu] must NOT change how UISP is reported (FR-018, SC-008).

    Render `obi status` with UISP alone, then with UISP + Siklu, and assert the UISP section's
    output is byte-for-byte identical (the Siklu section is simply appended).
    """

    monkeypatch.setenv("COLUMNS", "240")
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    dir_b = tmp_path / "b"
    dir_b.mkdir()

    # 1) UISP-only config.
    patch_transport(monkeypatch)
    uisp_only = write_uisp_config(dir_a)
    uisp_result = runner.invoke(app, ["status", "--scope", str(uisp_only)])
    assert uisp_result.exit_code == 0, uisp_result.output

    # 2) UISP + Siklu config (Siklu appended as its own section).
    patch_sessions(monkeypatch)
    both = write_uisp_config(
        dir_b,
        extra=(
            '[siklu]\nhosts = ["172.18.89.130"]\nusername = "admin"\npassword = "RadioSecret"\n'
        ),
    )
    both_result = runner.invoke(app, ["status", "--scope", str(both)])
    assert both_result.exit_code == 0, both_result.output

    # The UISP section (everything before the Siklu section) is unchanged.
    uisp_section = both_result.output.split("Siklu EtherHaul radios")[0]
    assert uisp_result.output.strip() in uisp_section
    # And the Siklu section is present in the combined run.
    assert "Siklu EtherHaul radios" in both_result.output
