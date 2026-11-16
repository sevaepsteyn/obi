"""T013: end-to-end `obi snmp` against an injected fake transport (feature 012).

A system sweep renders per-device blocks plus an explicit failure line for a down device; --json
validates against the report schema; a not-configured run exits cleanly with guidance.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.fakes import FakeSnmpAgent
from tests.integration._snmp import (
    COMMUNITY,
    patch_factory,
    save_roster,
    system_agent,
    write_snmp_config,
)

runner = CliRunner()

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "schemas" / "snmp-report.schema.json"
)


def test_snmp_system_sweep_reports_each_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_snmp_config(tmp_path)
    save_roster(tmp_path, [("172.18.84.1", "core-gw-1"), ("172.18.84.2", "core-gw-2")])
    # core-gw-1 answers; core-gw-2 is down → explicit per-device failure, sweep still completes.
    patch_factory(
        monkeypatch,
        {"172.18.84.1": system_agent(name="core-gw-1")},  # no agent for .0.2 → unreachable
    )

    result = runner.invoke(app, ["metrics", "live", "--source", "mikrotik", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "core-gw-1" in result.output
    assert "sysName=core-gw-1" in result.output
    # The per-source label must survive rendering (Rich must not eat the [snmp/...] brackets).
    assert "[snmp/mikrotik-roster]" in result.output
    assert "unreachable" in result.output  # the down device is reported, not dropped
    assert COMMUNITY not in result.output  # SC-004


def test_snmp_json_conforms_to_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_snmp_config(tmp_path)
    save_roster(tmp_path, [("172.18.84.1", "core-gw-1")])
    patch_factory(monkeypatch, {"172.18.84.1": system_agent()})

    result = runner.invoke(
        app, ["metrics", "live", "--source", "mikrotik", "--json", "--scope", str(config)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    schema = json.loads(_SCHEMA_PATH.read_text())
    assert payload["reports"]
    for report in payload["reports"]:
        jsonschema.validate(instance=report, schema=schema)
    assert COMMUNITY not in result.output


def test_snmp_not_configured_exits_with_guidance(tmp_path: Path) -> None:
    # A config without an [snmp] section.
    config = tmp_path / "config.toml"
    config.write_text(
        '[discovery]\nseeds = ["172.18.84.1"]\n[routeros]\n'
        'username = "admin"\npassword = "fakesecret44028f"\n'
    )
    config.chmod(0o600)

    result = runner.invoke(app, ["metrics", "live", "--scope", str(config)])

    assert result.exit_code == 2
    assert "snmp not configured" in result.output.lower()


def test_snmp_down_agent_never_aborts_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obi.core.models import SnmpOutcome

    config = write_snmp_config(tmp_path)
    save_roster(tmp_path, [("172.18.84.1", "core-gw-1"), ("172.18.84.2", "edge-2")])
    patch_factory(
        monkeypatch,
        {
            "172.18.84.1": system_agent(name="core-gw-1"),
            "172.18.84.2": FakeSnmpAgent(fail=SnmpOutcome.TIMED_OUT),
        },
    )

    result = runner.invoke(app, ["metrics", "live", "--source", "mikrotik", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    assert "core-gw-1" in result.output and "timed_out" in result.output
