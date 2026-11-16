"""T009: end-to-end `obi status` for a UniFi-only config (live, fixture-backed) — feature 011."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers.unifi_common import UnifiError
from obi.core.models import SourceOutcome
from tests.integration._unifi import patch_transports, write_unifi_config

runner = CliRunner()


def test_status_lists_unifi_devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transports(monkeypatch)
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "udm-ridge" in out  # device listed without manual enumeration (SC-001)
    assert "10.40.0.1" in out
    assert "UDM-Pro" in out  # official model (authoritative over controller "UDMPRO")
    assert "3.2.7" in out  # firmware version
    assert "unifi" in out  # vendor label
    assert "uap-shade" in out  # offline device still listed
    assert "uap-attic" in out  # controller-only device merged in
    assert "UniFi device(s)" in out


def test_status_json_devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transports(monkeypatch)
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["console_outcome"] == "reachable"
    assert payload["console"] == "https://console.unifi-hosting.example.com"
    assert all(d["vendor"] == "unifi" for d in payload["devices"])
    addresses = {d["address"] for d in payload["devices"]}
    assert {"10.40.0.1", "10.40.0.2", "10.40.0.3"} <= addresses
    assert len(payload["devices"]) == 4  # 2 merged + 1 official-only + 1 controller-only


def test_status_writes_unifi_audit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transports(monkeypatch)
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    audit_lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert sum(1 for ln in audit_lines if '"unifi_status"' in ln) == 1


def test_status_auth_failure_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transports(
        monkeypatch,
        official_error=UnifiError(SourceOutcome.AUTH_FAILED, "integration API rejected (401)"),
        controller_error=UnifiError(SourceOutcome.AUTH_FAILED, "controller API rejected (401)"),
    )
    config = write_unifi_config(tmp_path)

    result = runner.invoke(app, ["status", "--scope", str(config)])

    assert result.exit_code == 0, result.output  # reported, not raised — obi exits cleanly
    assert "auth_failed" in result.output
    assert "Traceback" not in result.output
