"""Integration: the `obi siklu status` / `obi siklu links` commands (feature 020)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers.siklu import SikluAuthError
from tests.integration._siklu import patch_sessions, write_siklu_config

runner = CliRunner()

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "tests" / "schemas" / "siklu-status-output.schema.json"
    ).read_text()
)


def test_siklu_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_sessions(monkeypatch)
    config = write_siklu_config(tmp_path)

    result = runner.invoke(app, ["siklu", "status", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "tamarack-ptp-basswood-70" in result.output
    assert "basswood-ptp-tamarack-70" in result.output
    assert "siklu" in result.output  # vendor label
    assert "Siklu radio(s)" in result.output
    # The credential never appears in operator-facing output.
    assert "RadioSecret" not in result.output


def test_siklu_json_conforms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_sessions(monkeypatch)
    config = write_siklu_config(tmp_path)

    result = runner.invoke(app, ["siklu", "status", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    jsonschema.validate(instance=payload, schema=_SCHEMA)
    assert payload["counts"]["reachable"] == 2
    serials = {r["serial"] for r in payload["radios"]}
    assert serials == {"2B058C1831BA", "CDA66BC2B74A"}
    # ONE siklu_status audit record; credential never audited.
    audit = (tmp_path / "audit.jsonl").read_text()
    assert audit.count('"siklu_status"') == 1
    assert "RadioSecret" not in audit


def test_siklu_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_sessions(monkeypatch)
    config = write_siklu_config(tmp_path)

    result = runner.invoke(app, ["siklu", "links", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "basswood↔tamarack-70" in result.output
    assert "up" in result.output
    assert "link(s)" in result.output


def test_siklu_links_json_no_asymmetry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_sessions(monkeypatch)
    config = write_siklu_config(tmp_path)

    result = runner.invoke(app, ["siklu", "links", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    jsonschema.validate(instance=payload, schema=_SCHEMA)
    assert len(payload["links"]) == 1
    # The qam128/qam32 healthy-link asymmetry is NOT flagged.
    assert payload["links"][0]["state"] == "up"
    assert payload["links"][0]["asymmetries"] == []


def test_siklu_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_sessions(monkeypatch)
    config = write_siklu_config(tmp_path)

    result = runner.invoke(
        app, ["siklu", "status", "--scope", str(config), "--filter", "tamarack-ptp"]
    )

    assert result.exit_code == 0, result.output
    assert "tamarack-ptp-basswood-70" in result.output
    assert "basswood-ptp-tamarack-70" not in result.output


def test_siklu_unreachable_auth_timeout_mix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One reachable, one auth-failed, one timed-out — all reported, run exits cleanly (FR-007).
    patch_sessions(
        monkeypatch,
        failures={
            "172.25.110.2": SikluAuthError("bad password"),
            "172.25.110.3": TimeoutError("no answer"),
        },
    )
    config = write_siklu_config(tmp_path, hosts=["172.18.89.130", "172.25.110.2", "172.25.110.3"])

    result = runner.invoke(app, ["siklu", "status", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    by_addr = {r["address"]: r for r in payload["radios"]}
    assert by_addr["172.18.89.130"]["reachability"] == "reachable"
    assert by_addr["172.25.110.2"]["reachability"] == "auth_failed"
    assert by_addr["172.25.110.3"]["reachability"] == "timed_out"


def test_siklu_errors_when_not_configured(tmp_path: Path) -> None:
    # A config with [siklu] hosts empty ⇒ siklu unconfigured ⇒ exit 2 + guidance, aligned with
    # the other vendor-scoped commands (supersedes 020's FR-010 exit-0 behavior, 2026-07-07).
    config = tmp_path / "config.toml"
    config.write_text(
        '[uisp]\nbase_url = "https://u"\ntoken = "t"\n[siklu]\nhosts = []\n'
        f'[audit]\npath = "{tmp_path / "audit.jsonl"}"\n'
    )
    config.chmod(0o600)

    result = runner.invoke(app, ["siklu", "status", "--scope", str(config)])

    assert result.exit_code == 2
    assert "siklu not configured" in result.output
