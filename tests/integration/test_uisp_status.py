"""T011: end-to-end `obi status` for a UISP-only config (live, fixture-backed)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.integration._uisp import patch_transport, write_uisp_config

runner = CliRunner()

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "schemas" / "uisp-status-output.schema.json"
)


def test_status_lists_uisp_devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "ptp-ridge-east" in out  # SC-001: device listed without manual enumeration
    assert "10.30.1.5" in out
    assert "PowerBeam 5AC Gen2" in out  # model
    assert "WA.v8.7.11" in out  # firmware version
    assert "uisp" in out  # vendor label (FR-006)
    assert "ptp-ridge-west" in out  # SC-002: offline device still listed (FR-004)
    assert "UISP device(s)" in out


def test_status_ignore_names_excludes_matching_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """feature 035: [uisp] ignore_names drops a matching device by identity; others remain."""
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path, extra='ignore_names = ["ptp-ridge-west"]')

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "ptp-ridge-west" not in result.output  # out-of-scope device excluded
    assert "ptp-ridge-east" in result.output  # in-scope device unaffected


def test_status_json_conforms_to_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    jsonschema.validate(instance=payload, schema=json.loads(_SCHEMA_PATH.read_text()))
    assert payload["controller_outcome"] == "reachable"
    assert payload["controller"] == "https://uisp.example.com"
    assert {d["address"] for d in payload["devices"]} >= {"10.30.1.5", "10.30.0.2"}
    assert all(d["vendor"] == "uisp" for d in payload["devices"])


def test_status_writes_uisp_audit_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert any('"uisp_status"' in line for line in audit_lines)
