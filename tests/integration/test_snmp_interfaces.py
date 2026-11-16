"""T021 (US2): `obi metrics live --profile interfaces` renders per-interface rows (012)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from tests.integration._snmp import (
    COMMUNITY,
    interfaces_agent,
    patch_factory,
    save_roster,
    write_snmp_config,
)

runner = CliRunner()


def test_interfaces_profile_lists_each_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_snmp_config(tmp_path)
    save_roster(tmp_path, [("172.18.84.1", "core-gw-1")])
    patch_factory(monkeypatch, {"172.18.84.1": interfaces_agent()})

    result = runner.invoke(
        app,
        ["metrics", "live", "--profile", "interfaces", "--source", "mikrotik"]
        + ["--scope", str(config)],
    )

    assert result.exit_code == 0, result.output
    assert "ether1" in result.output and "ether2" in result.output
    assert "ifHCInOctets=1000000" in result.output
    assert "ifOperStatus=1" in result.output
    assert COMMUNITY not in result.output
