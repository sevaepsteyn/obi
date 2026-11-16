"""Integration (feature 096, US3): probe → store → `obi health status`, through the CLI.

The default SSH transport is monkeypatched to serve the recorded core-vms-1 fixture (the
_siklu.py pattern), so the full path — config parse, engine fan-out, store write, fleet
view — runs exactly as production wires it, offline.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers.linux_health import LinuxHealthDriver

runner = CliRunner(env={"COLUMNS": "200"})

_FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "linux_health" / "core-vms-1.json").read_text()
)["commands"]


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        "[logs]\n"
        f'mirror_root = "{tmp_path / "mirror"}"\n'
        f'db_path = "{tmp_path / "logs.db"}"\n'
        "\n[snmp]\n"
        'community = "public"\n'
        "\n[snmp.history]\n"
        f'db_path = "{tmp_path / "metrics.db"}"\n'
        "\n[health.linux]\n"
        'username = "probe"\n'
        'hosts = ["core-vms-1", "magnolia-vms-1"]\n'
        "\n[audit]\n"
        f'path = "{tmp_path / "audit.jsonl"}"\n'
    )
    config.chmod(0o600)
    return config


def _patch_transport(monkeypatch) -> None:
    def fake_exec(self, host: str, command: str, timeout: float) -> str:
        if host != "core-vms-1":
            raise ConnectionRefusedError("host down")
        return _FIXTURE.get(command, "")

    monkeypatch.setattr(LinuxHealthDriver, "_ssh_exec", fake_exec)


def test_probe_then_status_shows_the_collector_fleet(tmp_path: Path, monkeypatch) -> None:
    config = _write_config(tmp_path)
    _patch_transport(monkeypatch)

    probe = runner.invoke(app, ["health", "probe", "--scope", str(config)])
    assert probe.exit_code == 0, probe.output
    assert "1 ok" in probe.output
    assert "magnolia-vms-1: unreachable" in probe.output

    status = runner.invoke(app, ["health", "status", "--scope", str(config), "--json"])
    payload = json.loads(status.output)
    devices = {d["device"]: d for d in payload["devices"]}
    assert "core-vms-1" in devices
    assert devices["core-vms-1"]["kind"] == "linux"
    metrics = {r["metric"] for r in devices["core-vms-1"]["readings"]}
    assert {"linux:cpuUtilPercent", "linux:memUsedPercent", "linux:diskUsedPercent"} <= metrics
    assert "magnolia-vms-1" not in devices  # a dead probe stores nothing — honest absence


def test_probe_without_section_exits_with_guidance(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[logs]\nmirror_root = "{tmp_path / "m"}"\ndb_path = "{tmp_path / "l.db"}"\n'
    )
    config.chmod(0o600)
    result = runner.invoke(app, ["health", "probe", "--scope", str(config)])
    assert result.exit_code == 2
    assert "health.linux" in result.output
